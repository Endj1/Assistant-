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

# Простая память последних сообщений на чат (сбрасывается при перезапуске)
chat_history = {}
MAX_HISTORY = 6  # сколько последних сообщений помнить


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
    # Бесплатная генерация изображений через Pollinations.ai, без ключей и регистрации
    encoded = requests.utils.quote(prompt)
    return f"https://image.pollinations.ai/prompt/{encoded}?width=768&height=768&nologo=true"


def send_message(chat_id: int, text: str):
    requests.post(
        f"{TELEGRAM_API}/sendMessage",
        json={"chat_id": chat_id, "text": text},
        timeout=15,
    )


def send_photo(chat_id: int, photo_url: str, caption: str = ""):
    requests.post(
        f"{TELEGRAM_API}/sendPhoto",
        json={"chat_id": chat_id, "photo": photo_url, "caption": caption[:1000]},
        timeout=60,
    )


@app.route("/webhook", methods=["POST"])
def webhook():
    update = request.get_json(force=True)
    message = update.get("message")

    if message and "text" in message:
        chat_id = message["chat"]["id"]
        user_text = message["text"].strip()

        # Команда генерации картинки: /image текст запроса
        if user_text.startswith("/image"):
            prompt = user_text[len("/image"):].strip()
            if not prompt:
                send_message(
                    chat_id,
                    "Напишите запрос после команды, например:\n/image кот в космосе",
                )
            else:
                try:
                    photo_url = generate_image_url(prompt)
                    send_photo(chat_id, photo_url, caption=prompt)
                except Exception as e:
                    send_message(chat_id, "Не удалось создать картинку, попробуйте ещё раз.")
                    print("Ошибка генерации картинки:", e)
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
