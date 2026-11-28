import os
import re
import time
import base64
import threading
from xml.etree import ElementTree

import requests
from flask import Flask, request

app = Flask(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
GROQ_API_KEY = os.environ["GROQ_API_KEY"]
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID")  # ваш личный chat_id, узнать через /whoami

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
GROQ_API = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "openai/gpt-oss-120b"
GROQ_VISION_MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"

# ---------------------------------------------------------------------------
# Системная инструкция — поведение бота. Отредактируйте под себя.
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = (
    "Ты — дружелюбный помощник, который отвечает на сообщения в Telegram "
    "от имени владельца аккаунта. Отвечай кратко, вежливо и по делу."
)

BTN_REMIND = "⏰ Напоминание"
BTN_WEATHER = "🌤 Погода"
BTN_NEWS = "📰 Новости"
BTN_CONVERT = "🔄 Конвертер"
BTN_QR = "📱 QR-код"

STYLES = {
    "style_none": "",
    "style_realism": "photorealistic, highly detailed, professional photography, natural lighting",
    "style_anime": "anime style, vibrant colors, clean lines, studio quality anime art",
    "style_watercolor": "watercolor painting, soft brush strokes, artistic, delicate colors",
}
STYLE_LABELS = {
    "style_none": "Без стиля",
    "style_realism": "📷 Реализм",
    "style_anime": "🎌 Аниме",
    "style_watercolor": "🖌 Акварель",
}

# ---------------------------------------------------------------------------
# Память в оперативной памяти процесса (сбрасывается при перезапуске/сне сервиса)
# ---------------------------------------------------------------------------
chat_history = {}
MAX_HISTORY = 6

awaiting_image_prompt = {}        # chat_id -> выбранный стиль, ждём описание
awaiting_weather = set()          # ждём название города
awaiting_reminder = set()         # ждём текст напоминания
awaiting_qr = set()               # ждём текст/ссылку для QR-кода

reminders = []  # список словарей: {chat_id, due_ts, text}
reminders_lock = threading.Lock()

known_users = set()  # все chat_id, кто писал боту — для рассылки (/broadcast)

stats = {"messages": 0, "images": 0}  # простая статистика для /stats (только для админа)


# ---------------------------------------------------------------------------
# Вспомогательные функции Telegram
# ---------------------------------------------------------------------------
def main_keyboard():
    return {
        "keyboard": [
            [{"text": BTN_WEATHER}, {"text": BTN_NEWS}],
            [{"text": BTN_REMIND}, {"text": BTN_CONVERT}],
            [{"text": BTN_QR}],
        ],
        "resize_keyboard": True,
    }


def send_message(chat_id, text, with_keyboard=False, reply_markup=None):
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    elif with_keyboard:
        payload["reply_markup"] = main_keyboard()
    requests.post(f"{TELEGRAM_API}/sendMessage", json=payload, timeout=15)


def send_photo(chat_id, photo_url, caption=""):
    requests.post(
        f"{TELEGRAM_API}/sendPhoto",
        json={
            "chat_id": chat_id,
            "photo": photo_url,
            "caption": caption[:1000],
            "reply_markup": main_keyboard(),
        },
        timeout=60,
    )


def answer_callback(callback_id):
    requests.post(
        f"{TELEGRAM_API}/answerCallbackQuery",
        json={"callback_query_id": callback_id},
        timeout=10,
    )


def style_keyboard():
    row = [{"text": label, "callback_data": key} for key, label in STYLE_LABELS.items()]
    return {"inline_keyboard": [[r] for r in row]}


# ---------------------------------------------------------------------------
# ИИ-чат (Groq)
# ---------------------------------------------------------------------------
def ask_ai(chat_id, user_text, system=SYSTEM_PROMPT, remember=True):
    history = chat_history.get(chat_id, []) if remember else []
    history.append({"role": "user", "content": user_text})
    history = history[-MAX_HISTORY:]

    messages = [{"role": "system", "content": system}] + history

    resp = requests.post(
        GROQ_API,
        headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
        json={"model": GROQ_MODEL, "messages": messages, "temperature": 0.7, "max_tokens": 500},
        timeout=30,
    )
    resp.raise_for_status()
    reply = resp.json()["choices"][0]["message"]["content"]

    if remember:
        history.append({"role": "assistant", "content": reply})
        chat_history[chat_id] = history[-MAX_HISTORY:]
    return reply


def enhance_image_prompt(raw_prompt: str, style_suffix: str) -> str:
    """Просим ИИ превратить короткое описание в подробный промпт для генерации картинки (на английском)."""
    instruction = (
        "You rewrite short user requests into a single detailed English prompt for an AI image "
        "generator. Add concrete visual details (composition, lighting, mood, colors) in 1-2 "
        "sentences. Reply with ONLY the improved prompt, nothing else, no quotes."
    )
    try:
        enhanced = ask_ai(chat_id=-1, user_text=raw_prompt, system=instruction, remember=False)
        enhanced = enhanced.strip().strip('"')
    except Exception as e:
        print("Ошибка улучшения промпта:", e)
        enhanced = raw_prompt
    if style_suffix:
        enhanced = f"{enhanced}, {style_suffix}"
    return enhanced


def generate_image_url(prompt: str) -> str:
    encoded = requests.utils.quote(prompt)
    return (
        f"https://image.pollinations.ai/prompt/{encoded}"
        f"?width=1024&height=1024&model=flux&nologo=true&enhance=true"
    )


def handle_image_generation(chat_id, raw_prompt, style_key):
    style_suffix = STYLES.get(style_key, "")
    send_message(chat_id, "Рисую, подождите немного...")
    try:
        final_prompt = enhance_image_prompt(raw_prompt, style_suffix)
        photo_url = generate_image_url(final_prompt)
        send_photo(chat_id, photo_url, caption=raw_prompt)
        stats["images"] += 1
    except Exception as e:
        send_message(chat_id, "Не удалось создать картинку, попробуйте ещё раз.", with_keyboard=True)
        print("Ошибка генерации картинки:", e)


# ---------------------------------------------------------------------------
# Понимание картинок (Groq, модель с поддержкой зрения)
# ---------------------------------------------------------------------------
def download_telegram_photo_b64(file_id: str) -> str:
    file_info = requests.get(f"{TELEGRAM_API}/getFile", params={"file_id": file_id}, timeout=15).json()
    file_path = file_info["result"]["file_path"]
    file_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"
    img_resp = requests.get(file_url, timeout=30)
    img_resp.raise_for_status()
    return base64.b64encode(img_resp.content).decode()


def analyze_image(b64_image: str, question: str) -> str:
    resp = requests.post(
        GROQ_API,
        headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
        json={
            "model": GROQ_VISION_MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": question},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64_image}"}},
                    ],
                }
            ],
            "max_tokens": 600,
        },
        timeout=45,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def handle_photo_message(chat_id, file_id, caption):
    question = caption.strip() if caption else "Опиши, что изображено на этой картинке, подробно и на русском языке."
    try:
        b64_image = download_telegram_photo_b64(file_id)
        answer = analyze_image(b64_image, question)
        send_message(chat_id, answer, with_keyboard=True)
    except Exception as e:
        send_message(chat_id, "Не удалось обработать картинку, попробуйте ещё раз.", with_keyboard=True)
        print("Ошибка анализа картинки:", e)


# ---------------------------------------------------------------------------
# Погода (wttr.in, бесплатно, без ключа и без общего дневного лимита)
# ---------------------------------------------------------------------------
def get_weather(city: str) -> str:
    resp = requests.get(
        f"https://wttr.in/{requests.utils.quote(city)}",
        params={"format": "3", "lang": "ru", "M": ""},
        headers={"User-Agent": "curl/8.0"},  # wttr.in лучше отвечает с таким заголовком
        timeout=15,
    )
    text = resp.text.strip()

    if not text or "Unknown location" in text or resp.status_code != 200:
        print("Пустой/некорректный ответ погоды:", resp.status_code, text)
        return f"Не нашёл город «{city}», проверьте название, или сервис временно недоступен."

    return f"Погода: {text}"


# ---------------------------------------------------------------------------
# Конвертер валют и единиц измерения
# ---------------------------------------------------------------------------
CURRENCY_ALIASES = {
    "usd": "USD", "доллар": "USD", "доллара": "USD", "долларов": "USD", "доллары": "USD", "баксов": "USD", "баксы": "USD", "$": "USD",
    "eur": "EUR", "евро": "EUR", "€": "EUR",
    "rub": "RUB", "руб": "RUB", "рубль": "RUB", "рубля": "RUB", "рублей": "RUB", "рубли": "RUB", "₽": "RUB",
    "gbp": "GBP", "фунт": "GBP", "фунта": "GBP", "фунтов": "GBP", "фунты": "GBP",
    "jpy": "JPY", "иена": "JPY", "йена": "JPY", "иен": "JPY", "иены": "JPY",
    "cny": "CNY", "юань": "CNY", "юаня": "CNY", "юаней": "CNY", "юани": "CNY",
    "chf": "CHF", "франк": "CHF", "франка": "CHF", "франков": "CHF", "франки": "CHF",
    "kzt": "KZT", "тенге": "KZT",
    "uah": "UAH", "гривна": "UAH", "гривны": "UAH", "гривен": "UAH", "гривны": "UAH",
    "try": "TRY", "лира": "TRY", "лиры": "TRY", "лир": "TRY",
}

# категория -> {единица: коэффициент пересчёта в базовую единицу категории}
UNIT_CATEGORIES = {
    "length": {  # база — метр
        "km": 1000, "км": 1000, "m": 1, "м": 1, "cm": 0.01, "см": 0.01, "mm": 0.001, "мм": 0.001,
        "mile": 1609.34, "miles": 1609.34, "миля": 1609.34, "мили": 1609.34, "милю": 1609.34,
        "ft": 0.3048, "foot": 0.3048, "фут": 0.3048, "футов": 0.3048, "фута": 0.3048,
        "in": 0.0254, "inch": 0.0254, "дюйм": 0.0254, "дюйма": 0.0254, "дюймов": 0.0254,
        "yd": 0.9144, "yard": 0.9144, "ярд": 0.9144, "ярда": 0.9144, "ярдов": 0.9144,
    },
    "weight": {  # база — килограмм
        "kg": 1, "кг": 1, "g": 0.001, "г": 0.001, "гр": 0.001, "грамм": 0.001, "граммов": 0.001, "граммы": 0.001,
        "lb": 0.453592, "lbs": 0.453592, "фунт": 0.453592, "фунта": 0.453592, "фунтов": 0.453592, "фунты": 0.453592,
        "oz": 0.0283495, "унция": 0.0283495, "унций": 0.0283495, "унции": 0.0283495,
        "тонна": 1000, "тонны": 1000, "тонн": 1000, "t": 1000,
    },
    "volume": {  # база — литр
        "l": 1, "л": 1, "литр": 1, "литра": 1, "литров": 1,
        "ml": 0.001, "мл": 0.001,
        "gal": 3.78541, "галлон": 3.78541, "галлона": 3.78541, "галлонов": 3.78541,
    },
}


def normalize_token(tok: str) -> str:
    return tok.strip().lower().rstrip(".,!?")


def convert_units(amount: float, from_tok: str, to_tok: str):
    """Пробует сконвертировать как единицы измерения. Возвращает число или None."""
    f, t = normalize_token(from_tok), normalize_token(to_tok)

    # Температура — особый случай (не линейный коэффициент)
    temp_c = {"c", "°c", "цельсий", "цельсия"}
    temp_f = {"f", "°f", "фаренгейт", "фаренгейта"}
    if f in temp_c and t in temp_f:
        return amount * 9 / 5 + 32
    if f in temp_f and t in temp_c:
        return (amount - 32) * 5 / 9

    for factors in UNIT_CATEGORIES.values():
        if f in factors and t in factors:
            return amount * factors[f] / factors[t]
    return None


def convert_currency(amount: float, from_tok: str, to_tok: str):
    """Пробует сконвертировать как валюту. Возвращает число или None."""
    f_code = CURRENCY_ALIASES.get(normalize_token(from_tok))
    t_code = CURRENCY_ALIASES.get(normalize_token(to_tok))
    if not f_code or not t_code:
        return None
    data = requests.get(f"https://open.er-api.com/v6/latest/{f_code}", timeout=15).json()
    rate = (data.get("rates") or {}).get(t_code)
    if rate is None:
        return None
    return amount * rate


# ---------------------------------------------------------------------------
# Новости (RSS, бесплатно, без ключа)
# ---------------------------------------------------------------------------
def get_news() -> str:
    resp = requests.get("https://lenta.ru/rss/news", timeout=15)
    root = ElementTree.fromstring(resp.content)
    items = root.findall(".//item")[:5]
    if not items:
        return "Не удалось получить новости, попробуйте позже."
    lines = ["📰 Свежие новости:\n"]
    for item in items:
        title = item.findtext("title", default="").strip()
        link = item.findtext("link", default="").strip()
        lines.append(f"• {title}\n{link}")
    return "\n\n".join(lines)


# ---------------------------------------------------------------------------
# QR-код (api.qrserver.com, бесплатно, без ключа)
# ---------------------------------------------------------------------------
def generate_qr_url(text: str) -> str:
    encoded = requests.utils.quote(text)
    return f"https://api.qrserver.com/v1/create-qr-code/?size=400x400&data={encoded}"


# ---------------------------------------------------------------------------
# Голосовые сообщения (Groq Whisper, бесплатно, в рамках того же GROQ_API_KEY)
# ---------------------------------------------------------------------------
GROQ_TRANSCRIBE_API = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_WHISPER_MODEL = "whisper-large-v3-turbo"


def download_telegram_file_bytes(file_id: str) -> bytes:
    file_info = requests.get(f"{TELEGRAM_API}/getFile", params={"file_id": file_id}, timeout=15).json()
    file_path = file_info["result"]["file_path"]
    file_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"
    resp = requests.get(file_url, timeout=30)
    resp.raise_for_status()
    return resp.content


def transcribe_voice(file_id: str) -> str:
    audio_bytes = download_telegram_file_bytes(file_id)
    resp = requests.post(
        GROQ_TRANSCRIBE_API,
        headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
        files={"file": ("voice.ogg", audio_bytes, "audio/ogg")},
        data={"model": GROQ_WHISPER_MODEL, "response_format": "json"},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json().get("text", "").strip()


# ---------------------------------------------------------------------------
# Напоминания — фоновый поток проверяет каждые 30 секунд.
# ВАЖНО: работает только пока сервис не "уснул" (ограничение бесплатного Render).
# ---------------------------------------------------------------------------
def parse_reminder(text: str):
    match = re.match(r"^\s*(\d+)\s+(.+)$", text)
    if not match:
        return None
    minutes = int(match.group(1))
    body = match.group(2).strip()
    return minutes, body


def reminder_worker():
    while True:
        now = time.time()
        due = []
        with reminders_lock:
            still_pending = []
            for r in reminders:
                if r["due_ts"] <= now:
                    due.append(r)
                else:
                    still_pending.append(r)
            reminders[:] = still_pending
        for r in due:
            send_message(r["chat_id"], f"⏰ Напоминание: {r['text']}", with_keyboard=True)
        time.sleep(30)


threading.Thread(target=reminder_worker, daemon=True).start()


# ---------------------------------------------------------------------------
# Основной обработчик вебхука
# ---------------------------------------------------------------------------
@app.route("/webhook", methods=["POST"])
def webhook():
    update = request.get_json(force=True)

    message = update.get("message")
    if not message:
        return "ok"

    # --- Пользователь прислал фото — анализируем через ИИ ---
    if "photo" in message:
        chat_id = message["chat"]["id"]
        known_users.add(chat_id)
        stats["messages"] += 1
        photo_sizes = message["photo"]
        largest = photo_sizes[-1]  # Telegram отдаёт размеры по возрастанию
        caption = message.get("caption", "")
        handle_photo_message(chat_id, largest["file_id"], caption)
        return "ok"

    # --- Пользователь прислал голосовое сообщение — распознаём речь ---
    if "voice" in message:
        chat_id = message["chat"]["id"]
        known_users.add(chat_id)
        stats["messages"] += 1
        try:
            recognized = transcribe_voice(message["voice"]["file_id"])
        except Exception as e:
            send_message(chat_id, "Не удалось распознать голосовое сообщение, попробуйте ещё раз.", with_keyboard=True)
            print("Ошибка распознавания голоса:", e)
            return "ok"
        if not recognized:
            send_message(chat_id, "Не удалось разобрать речь, попробуйте сказать чётче.", with_keyboard=True)
            return "ok"
        process_text_message(chat_id, recognized, voice_note=True)
        return "ok"

    if "text" not in message:
        return "ok"

    chat_id = message["chat"]["id"]
    user_text = message["text"].strip()
    known_users.add(chat_id)
    stats["messages"] += 1
    process_text_message(chat_id, user_text)
    return "ok"


def process_text_message(chat_id, user_text, voice_note=False):
    if voice_note:
        send_message(chat_id, f"🎤 Распознано: «{user_text}»")

    # --- /whoami — узнать свой chat_id (нужен, чтобы настроить ADMIN_CHAT_ID) ---
    if user_text == "/whoami":
        send_message(chat_id, f"Ваш chat_id: {chat_id}")
        return "ok"

    # --- /stats — статистика бота, доступна только владельцу ---
    if user_text == "/stats":
        if ADMIN_CHAT_ID and str(chat_id) == str(ADMIN_CHAT_ID):
            send_message(
                chat_id,
                "📊 Статистика бота:\n"
                f"👥 Пользователей: {len(known_users)}\n"
                f"💬 Сообщений обработано: {stats['messages']}\n"
                f"🎨 Картинок сгенерировано: {stats['images']}\n"
                f"⏰ Активных напоминаний: {len(reminders)}",
            )
            return "ok"
        # для всех остальных команда как будто не существует — просто идём дальше

    # --- /broadcast — рассылка всем пользователям, доступна только владельцу ---
    if user_text.startswith("/broadcast"):
        if not ADMIN_CHAT_ID or str(chat_id) != str(ADMIN_CHAT_ID):
            # Для всех остальных команда выглядит как несуществующая
            pass
        else:
            text = user_text[len("/broadcast"):].strip()
            if not text:
                send_message(chat_id, "Напишите текст рассылки после команды, например:\n/broadcast Добавил новую функцию!")
            else:
                sent, failed = 0, 0
                for uid in known_users:
                    try:
                        send_message(uid, f"📢 {text}")
                        sent += 1
                    except Exception:
                        failed += 1
                send_message(chat_id, f"Рассылка отправлена. Успешно: {sent}, ошибок: {failed}")
            return "ok"

    # --- /start ---
    if user_text == "/start":
        awaiting_image_prompt.pop(chat_id, None)
        awaiting_weather.discard(chat_id)
        awaiting_reminder.discard(chat_id)
        awaiting_qr.discard(chat_id)
        send_message(
            chat_id,
            "Привет! Я ИИ-помощник. Пишите мне что угодно.\n\n"
            "Чтобы получить картинку — просто напишите (или скажите голосом) "
            "«нарисуй ...» и описание.\n\n"
            "Кнопками снизу можно узнать погоду, новости, "
            "сделать QR-код или поставить напоминание.\n\n"
            "Конвертер валют и единиц работает прямо по тексту — просто "
            "напишите, например: «100 USD в рубли» или «5 км в мили».\n\n"
            "Также можете прислать фото — я опишу, что на нём.",
            with_keyboard=True,
        )
        return "ok"

    # --- Команда /image (стиль не выбирается) ---
    if user_text.startswith("/image"):
        prompt = user_text[len("/image"):].strip()
        if not prompt:
            send_message(chat_id, "Напишите запрос после команды, например:\n/image кот в космосе")
        else:
            handle_image_generation(chat_id, prompt, "style_none")
        return "ok"

    # --- Ждём описание картинки после выбора стиля ---
    if chat_id in awaiting_image_prompt:
        style_key = awaiting_image_prompt.pop(chat_id)
        handle_image_generation(chat_id, user_text, style_key)
        return "ok"

    # --- Кнопка "Погода" ---
    if user_text == BTN_WEATHER:
        awaiting_weather.add(chat_id)
        send_message(chat_id, "Напишите город.")
        return "ok"

    if chat_id in awaiting_weather:
        awaiting_weather.discard(chat_id)
        try:
            send_message(chat_id, get_weather(user_text), with_keyboard=True)
        except Exception as e:
            send_message(chat_id, "Не удалось получить погоду, попробуйте позже.", with_keyboard=True)
            print("Ошибка погоды:", e)
        return "ok"

    # --- Кнопка "Новости" ---
    if user_text == BTN_NEWS:
        try:
            send_message(chat_id, get_news(), with_keyboard=True)
        except Exception as e:
            send_message(chat_id, "Не удалось получить новости, попробуйте позже.", with_keyboard=True)
            print("Ошибка новостей:", e)
        return "ok"

    # --- Кнопка "Конвертер" — просто подсказка, сам конвертер работает по тексту ниже ---
    if user_text == BTN_CONVERT:
        send_message(
            chat_id,
            "Просто напишите, что перевести, например:\n"
            "«100 USD в рубли»\n«5 км в мили»\n«20 C в F»\n«2 кг в фунты»",
            with_keyboard=True,
        )
        return "ok"

    # --- Конвертер валют/единиц по свободному тексту: "100 USD в рубли" ---
    convert_match = re.match(
        r"^\s*([\d]+(?:[.,]\d+)?)\s*([^\s\d]+)\s+(?:в|to|->)\s+([^\s\d]+)\s*$",
        user_text,
        flags=re.IGNORECASE,
    )
    if convert_match:
        amount = float(convert_match.group(1).replace(",", "."))
        from_unit, to_unit = convert_match.group(2), convert_match.group(3)

        result = None
        try:
            result = convert_currency(amount, from_unit, to_unit)
            if result is None:
                result = convert_units(amount, from_unit, to_unit)
        except Exception as e:
            print("Ошибка конвертера:", e)

        if result is not None:
            send_message(
                chat_id,
                f"{amount:g} {from_unit} = {result:.2f} {to_unit}",
                with_keyboard=True,
            )
            return "ok"
        # если не распознали — просто идём дальше (например, это был обычный вопрос к ИИ)

    # --- Кнопка "QR-код" ---
    if user_text == BTN_QR:
        awaiting_qr.add(chat_id)
        send_message(chat_id, "Напишите текст или ссылку для QR-кода.")
        return "ok"

    if chat_id in awaiting_qr:
        awaiting_qr.discard(chat_id)
        try:
            qr_url = generate_qr_url(user_text)
            send_photo(chat_id, qr_url, caption=user_text)
        except Exception as e:
            send_message(chat_id, "Не удалось создать QR-код, попробуйте ещё раз.", with_keyboard=True)
            print("Ошибка QR-кода:", e)
        return "ok"

    # --- Кнопка "Напоминание" ---
    if user_text == BTN_REMIND:
        awaiting_reminder.add(chat_id)
        send_message(chat_id, "Напишите: через сколько минут и текст.\nНапример: 30 позвонить маме")
        return "ok"

    if chat_id in awaiting_reminder:
        awaiting_reminder.discard(chat_id)
        parsed = parse_reminder(user_text)
        if not parsed:
            send_message(
                chat_id,
                "Не понял формат. Напишите число минут и текст через пробел, "
                "например: 30 позвонить маме",
                with_keyboard=True,
            )
        else:
            minutes, body = parsed
            with reminders_lock:
                reminders.append({"chat_id": chat_id, "due_ts": time.time() + minutes * 60, "text": body})
            send_message(chat_id, f"Хорошо, напомню через {minutes} мин: «{body}»", with_keyboard=True)
        return "ok"

    # --- Естественная просьба нарисовать (из текста или голоса), без кнопки/команды ---
    draw_match = re.search(
        r"(нарисуй|нарисовать|сгенерируй\s+картинку|создай\s+картинку)\s*(.*)",
        user_text,
        flags=re.IGNORECASE,
    )
    if draw_match:
        description = draw_match.group(2).strip(" .,:;!?")
        if description:
            handle_image_generation(chat_id, description, "style_none")
        else:
            awaiting_image_prompt[chat_id] = "style_none"
            send_message(chat_id, "Опишите, что нарисовать.")
        return "ok"

    # --- Обычный текстовый ответ через ИИ ---
    try:
        reply = ask_ai(chat_id, user_text)
    except Exception as e:
        reply = "Извините, сейчас не могу ответить. Попробуйте позже."
        print("Ошибка:", e)
    send_message(chat_id, reply)

    return "ok"


@app.route("/", methods=["GET"])
def health():
    return "Bot is running"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
