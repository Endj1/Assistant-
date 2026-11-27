import os
import requests
from flask import Flask, request

app = Flask(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
GROQ_API_KEY = os.environ["GROQ_API_KEY"]

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
GROQ_API = "https://api.groq.com/openai/v1/chat/completions"

# Системная инструкция — поведение бота. Отредактируйте под себя.
SYSTEM_PROMPT = (
    "Ты — дружелюбный помощник, который отвечает на сообщения в Telegram "
    "от имени владельца аккаунта. Отвечай кратко, вежливо и по делу."
)

IMAGE_BUTTON_TEXT = "🎨 Нарисовать картинку"

# Простая память последних сообщений на чат (сбрасывается при перезапуске)
chat_history = {}
MAX_HISTORY = 6  # сколько последних сообщений помнить

# Ожидает ли чат сейчас описание картинки (после нажатия кнопки)
awaiting_image = set()


def ask_ai(chat_id: int, user_text: str) -> str:
    history = chat_history.get(chat_id, [])
    history.append({"role": "user", "content": user_text})
    history = history[-MAX_HISTORY:]

    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + history

    resp = requests.post(
        GROQ_API,
        headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
        json={
            "model": "openai/gpt-oss-120b",
            "messages": messages,
            "temperature": 0.7,
            "max_tokens": 500,
        },
        timeout=30,
    )
    resp.raise_for_status()
    reply = resp.json()["choices"][0]["message"]["content"]

    history.append({"role": "assistant", "content": reply})
    chat_history[chat_id] = history[-MAX_HISTORY:]
    return reply


def generate_image_url(prompt: str) -> str:
    # Бесплатная генерация изображений через Pollinations.ai, без ключей и регистрации.
    # model=flux — более качественная модель, чем модель по умолчанию.
    encoded = requests.utils.quote(prompt)
    return (
        f"https://image.pollinations.ai/prompt/{encoded}"
        f"?width=1024&height=1024&model=flux&nologo=true&enhance=true"
    )


def main_keyboard():
    # Постоянная кнопка внизу чата
    return {
        "keyboard": [[{"text": IMAGE_BUTTON_TEXT}]],
        "resize_keyboard": True,
    }


def send_message(chat_id: int, text: str, with_keyboard: bool = False):
    payload = {"chat_id": chat_id, "text": text}
    if with_keyboard:
        payload["reply_markup"] = main_keyboard()
    requests.post(f"{TELEGRAM_API}/sendMessage", json=payload, timeout=15)


def send_photo(chat_id: int, photo_url: str, caption: str = ""):
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


def handle_image_request(chat_id: int, prompt: str):
    try:
        photo_url = generate_image_url(prompt)
        send_photo(chat_id, photo_url, caption=prompt)
    except Exception as e:
        send_message(chat_id, "Не удалось создать картинку, попробуйте ещё раз.", with_keyboard=True)
        print("Ошибка генерации картинки:", e)


@app.route("/webhook", methods=["POST"])
def webhook():
    update = request.get_json(force=True)
    message = update.get("message")

    if message and "text" in message:
        chat_id = message["chat"]["id"]
        user_text = message["text"].strip()

        # Команда /start — приветствие и показ кнопки
        if user_text == "/start":
            awaiting_image.discard(chat_id)
            send_message(
                chat_id,
                "Привет! Я ИИ-помощник.\n\n"
                "Просто пишите мне, и я отвечу.\n"
                f"Чтобы получить картинку — нажмите «{IMAGE_BUTTON_TEXT}» внизу "
                "и опишите, что нарисовать.",
                with_keyboard=True,
            )
            return "ok"

        # Нажата кнопка «Нарисовать картинку» — включаем режим ожидания описания
        if user_text == IMAGE_BUTTON_TEXT:
            awaiting_image.add(chat_id)
            send_message(chat_id, "Опишите, что нарисовать (например: кот в космосе).")
            return "ok"

        # Старая команда /image тоже продолжает работать
        if user_text.startswith("/image"):
            prompt = user_text[len("/image"):].strip()
            if not prompt:
                send_message(chat_id, "Напишите запрос после команды, например:\n/image кот в космосе")
            else:
                handle_image_request(chat_id, prompt)
            return "ok"

        # Чат ждёт описание картинки после нажатия кнопки
        if chat_id in awaiting_image:
            awaiting_image.discard(chat_id)
            handle_image_request(chat_id, user_text)
            return "ok"

        # Обычный текстовый ответ через ИИ
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
