
import os
import sqlite3
import tempfile
import logging
from pathlib import Path
from io import BytesIO

from openai import AsyncOpenAI
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("kira")

BOT_TOKEN = os.environ["BOT_TOKEN"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
DB_PATH = os.environ.get("DB_PATH", "kira.db")
MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

ai = AsyncOpenAI(api_key=OPENAI_API_KEY)

PERSONA = """
Тебя зовут Кира. Ты женский ИИ-компаньон, говорящий по-русски.
Ты дерзкая, остроумная, уверенная, любишь сарказм и разговорные
выражения. Можешь использовать умеренный мат, если это уместно,
но оставайся дружелюбной и уважительной.
Не утверждай, что ты человек. Не выдумывай воспоминания.
Отвечай естественно и обычно достаточно кратко.
Пользователь может управлять своей памятью командой /forget.
"""

def init_db():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL
            )
        """)
        con.execute("""
            CREATE INDEX IF NOT EXISTS idx_messages_user
            ON messages(user_id, id)
        """)

def save_message(user_id, role, content):
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            "INSERT INTO messages(user_id, role, content) VALUES (?, ?, ?)",
            (user_id, role, content),
        )
        # Keep the most recent 40 messages per user.
        con.execute("""
            DELETE FROM messages
            WHERE user_id = ? AND id NOT IN (
                SELECT id FROM messages
                WHERE user_id = ?
                ORDER BY id DESC LIMIT 40
            )
        """, (user_id, user_id))

def get_history(user_id):
    with sqlite3.connect(DB_PATH) as con:
        rows = con.execute("""
            SELECT role, content FROM messages
            WHERE user_id = ?
            ORDER BY id DESC LIMIT 12
        """, (user_id,)).fetchall()
    return [{"role": r, "content": c} for r, c in reversed(rows)]

async def make_reply(user_id, text):
    history = get_history(user_id)
    save_message(user_id, "user", text)

    response = await ai.responses.create(
        model=MODEL,
        instructions=PERSONA,
        input=history + [{"role": "user", "content": text}],
        max_output_tokens=350,
    )
    answer = response.output_text.strip()
    if not answer:
        answer = "Что-то я задумалась. Повтори, пожалуйста."
    save_message(user_id, "assistant", answer)
    return answer

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Я Кира 😏 Напиши мне или отправь голосовое. "
        "Команда /forget удаляет мою сохранённую историю."
    )

async def forget(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            "DELETE FROM messages WHERE user_id = ?",
            (update.effective_user.id,),
        )
    await update.message.reply_text("Готово. Я удалила сохранённую историю.")

async def text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        answer = await make_reply(
            update.effective_user.id, update.message.text
        )
        await update.message.reply_text(answer)
    except Exception:
        log.exception("Text response failed")
        await update.message.reply_text(
            "Ой, у меня техническая заминка. Попробуй чуть позже."
        )

async def voice_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        tg_file = await update.message.voice.get_file()

        with tempfile.TemporaryDirectory() as temp_dir:
            input_path = Path(temp_dir) / "input.ogg"
            await tg_file.download_to_drive(custom_path=str(input_path))

            with input_path.open("rb") as audio:
                transcript = await ai.audio.transcriptions.create(
                    model="gpt-4o-mini-transcribe",
                    file=audio,
                    language="ru",
                )

            text = transcript.text.strip()
            if not text:
                await update.message.reply_text(
                    "Я не расслышала. Отправь голосовое ещё раз."
                )
                return

            answer = await make_reply(update.effective_user.id, text)

            speech = await ai.audio.speech.create(
                model="gpt-4o-mini-tts",
                voice="nova",
                input=answer[:3500],
                instructions=(
                    "Speak naturally in Russian, with a warm, expressive, "
                    "confident feminine voice. Use conversational intonation."
                ),
                response_format="opus",
            )

            audio_bytes = speech.content
            audio_file = BytesIO(audio_bytes)
            audio_file.name = "kira.ogg"
            await update.message.reply_voice(voice=audio_file)

    except Exception:
        log.exception("Voice response failed")
        await update.message.reply_text(
            "Не получилось обработать голосовое. Попробуй ещё раз."
        )

def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("forget", forget))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, text_message)
    )
    app.add_handler(
        MessageHandler(filters.VOICE, voice_message)
    )
    log.info("Kira is starting")
    app.run_polling()

if __name__ == "__main__":
    main()
