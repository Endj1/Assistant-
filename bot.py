import os
import re
import time
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

# ---------------------------------------------------------------------------
# Системная инструкция — поведение бота. Отредактируйте под себя.
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = (
    "Ты — дружелюбный помощник, который отвечает на сообщения в Telegram "
    "от имени владельца аккаунта. Отвечай кратко, вежливо и по делу."
)

BTN_IMAGE = "🎨 Нарисовать картинку"
BTN_REMIND = "⏰ Напоминание"
BTN_WEATHER = "🌤 Погода"
BTN_NEWS = "📰 Новости"
BTN_CURRENCY = "💱 Курс валют"
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

awaiting_image_style = set()      # чат нажал "нарисовать", ждём выбор стиля
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
            [{"text": BTN_IMAGE}],
            [{"text": BTN_WEATHER}, {"text": BTN_NEWS}],
            [{"text": BTN_REMIND}, {"text": BTN_CURRENCY}],
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
# Курс валют (open.er-api.com, бесплатно, без ключа)
# ---------------------------------------------------------------------------
def get_currency_rates() -> str:
    data = requests.get("https://open.er-api.com/v6/latest/USD", timeout=15).json()
    rates = data.get("rates", {})
    usd_rub = rates.get("RUB")
    eur_usd = rates.get("EUR")
    if not usd_rub or not eur_usd:
        return "Не удалось получить курс валют, попробуйте позже."
    eur_rub = usd_rub / eur_usd
    return f"💱 Курс валют:\nUSD → {usd_rub:.2f} ₽\nEUR → {eur_rub:.2f} ₽"


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

    # Нажатие инлайн-кнопки выбора стиля
    callback = update.get("callback_query")
    if callback:
        chat_id = callback["message"]["chat"]["id"]
        data = callback.get("data", "")
        answer_callback(callback["id"])
        if data in STYLES:
            awaiting_image_style.discard(chat_id)
            awaiting_image_prompt[chat_id] = data
            send_message(chat_id, "Опишите, что нарисовать (например: кот в космосе).")
        return "ok"

    message = update.get("message")
    if not message or "text" not in message:
        return "ok"

    chat_id = message["chat"]["id"]
    user_text = message["text"].strip()
    known_users.add(chat_id)
    stats["messages"] += 1

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
        awaiting_image_style.discard(chat_id)
        awaiting_image_prompt.pop(chat_id, None)
        awaiting_weather.discard(chat_id)
        awaiting_reminder.discard(chat_id)
        awaiting_qr.discard(chat_id)
        send_message(
            chat_id,
            "Привет! Я ИИ-помощник. Пишите мне что угодно, а кнопками снизу "
            "можно нарисовать картинку, узнать погоду, новости, курс валют, "
            "сделать QR-код или поставить напоминание.",
            with_keyboard=True,
        )
        return "ok"

    # --- Кнопка "Нарисовать картинку" -> выбор стиля ---
    if user_text == BTN_IMAGE:
        awaiting_image_style.add(chat_id)
        send_message(chat_id, "Выберите стиль картинки:", reply_markup=style_keyboard())
        return "ok"

    # --- Команда /image без кнопок (стиль не выбирается) ---
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

    # --- Кнопка "Курс валют" ---
    if user_text == BTN_CURRENCY:
        try:
            send_message(chat_id, get_currency_rates(), with_keyboard=True)
        except Exception as e:
            send_message(chat_id, "Не удалось получить курс валют, попробуйте позже.", with_keyboard=True)
            print("Ошибка курса валют:", e)
        return "ok"

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
