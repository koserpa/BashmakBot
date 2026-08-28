import asyncio
import io
import logging
import re
from collections import defaultdict, deque
from pathlib import Path

import docx
import openpyxl
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatAction, ParseMode
from aiogram.filters import Command
from aiogram.types import Message
from duckduckgo_search import DDGS
from google import genai
from google.genai import errors, types
from pptx import Presentation

from config import (
    BOT_TOKEN,
    GEMINI_API_KEY,
    GEMINI_MODEL,
    TRIGGER_NAMES,
    SYSTEM_PROMPT,
    HISTORY_SIZE,
    USER_CONTEXT,
)

logging.basicConfig(level=logging.INFO)
logging.getLogger("google_genai.models").setLevel(logging.ERROR)
log = logging.getLogger("Bashma4ek_Bot")

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
ai_client = genai.Client(api_key=GEMINI_API_KEY)

# Словник тригерів, які вмикають вебпошук
SEARCH_TRIGGERS = {
    "знайди", "гугл", "пошукай", "новини", "погода",
    "курс", "сьогодні", "актуальн", "інтернет", "найди", "search", "google"
}


def needs_web_search(text: str) -> bool:
    """Перевіряє, чи містить запит слова-тригери для пошуку в інтернеті."""
    text_lower = text.lower()
    return any(trigger in text_lower for trigger in SEARCH_TRIGGERS)


def get_web_results(query: str) -> str:
    """Отримує результати з інтернету через DuckDuckGo безкоштовно та без лімітів API."""
    try:
        results = DDGS().text(query, max_results=3)
        if not results:
            return ""
        snippet = "\n\n[Знайдена актуальна інформація з інтернету]:\n"
        for r in results:
            snippet += f"- {r.get('title', '')}: {r.get('body', '')}\n"
        return snippet
    except Exception as e:
        log.error(f"Помилка пошуку DuckDuckGo: {e}")
        return ""


# chat_id -> deque of {"role": ..., "content": ...}
history: dict[int, deque] = defaultdict(lambda: deque(maxlen=HISTORY_SIZE))

_NAMES_ALT = "|".join(re.escape(n) for n in TRIGGER_NAMES)
NAME_PATTERN = re.compile(
    rf"(^\s*({_NAMES_ALT})\b)|(\b({_NAMES_ALT})\s*[,!?.]*\s*$)",
    re.IGNORECASE,
)


def strip_trigger(text: str, bot_username: str) -> str:
    """Прибирає @згадку бота та тригер-ім'я з тексту питання."""
    text = text.replace(f"@{bot_username}", "")
    text = NAME_PATTERN.sub("", text, count=1)
    return text.strip(" ,:.!?-")


def strip_name_prefix(text: str, sender: str, bot_name: str) -> str:
    """Прибирає префікс на кшталт 'koserpa: ' або 'Башмак: ' з відповіді моделі."""
    text = text.strip()
    names = "|".join(re.escape(n) for n in {sender, bot_name, *TRIGGER_NAMES} if n)
    text = re.sub(rf"^\s*(?:{names})\s*:\s*", "", text, count=1, flags=re.IGNORECASE)
    return text.strip()


def was_mentioned(message: Message, bot_username: str) -> bool:
    text = message.text or message.caption

    if message.reply_to_message and message.reply_to_message.from_user:
        if message.reply_to_message.from_user.id == bot.id:
            return True

    if not text:
        return False

    if f"@{bot_username}".lower() in text.lower():
        return True

    if NAME_PATTERN.search(text):
        return True

    return False


async def ask_gemini(contents: list) -> str:
    """Викликає Gemini API без нативних tools (щоб уникнути помилок 429)."""
    try:
        response = await ai_client.aio.models.generate_content(
            model=GEMINI_MODEL,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT
                + " У переписці повідомлення користувачів позначені як "
                  "'Ім'я: текст' — звертай увагу, хто саме що написав, "
                  "але у своїй відповіді імена дублювати не треба.",
                max_output_tokens=600,
                temperature=0.7,
            ),
        )
        return (response.text or "").strip()

    except errors.ClientError as e:
        if e.code == 429:
            log.warning("Запит відхилено: ліміт 429 (RESOURCE_EXHAUSTED)")
            return "Зараз отримую занадто багато запитів 🤯. Зачекай 1-2 хвилини!"
        log.error(f"Помилка Gemini API: {e}")
        return "Виникла помилка при зверненні до AI 😔"
    except Exception as e:
        log.error(f"Несподівана помилка в ask_gemini: {e}")
        return "Не вдалося сформулювати відповідь 😔"


async def download_photo_bytes(message: Message) -> bytes:
    """Завантажує найбільшу доступну версію фото з повідомлення."""
    photo = message.photo[-1]
    file = await bot.get_file(photo.file_id)
    buffer = await bot.download_file(file.file_path)
    return buffer.read()


def get_sender_context(message: Message) -> str:
    """Повертає підказку боту про відправника, якщо це відомий учасник."""
    if not message.from_user or not message.from_user.username:
        return ""
    username = message.from_user.username.lstrip("@").lower()
    for known_username, context in USER_CONTEXT.items():
        if known_username.lower() == username:
            return context
    return ""


MAX_DOC_CHARS = 40000


def extract_document_text(file_name: str, data: bytes, mime_type: str | None):
    """Готує вміст файлу для Gemini."""
    ext = Path(file_name or "").suffix.lower()
    mime_type = mime_type or ""

    if ext == ".pdf" or mime_type == "application/pdf":
        return None, types.Part.from_bytes(data=data, mime_type="application/pdf")

    if ext == ".docx" or "wordprocessingml.document" in mime_type:
        document = docx.Document(io.BytesIO(data))
        lines = [p.text for p in document.paragraphs if p.text.strip()]
        for table in document.tables:
            for row in table.rows:
                lines.append(" | ".join(cell.text for cell in row.cells))
        return "\n".join(lines), None

    if ext == ".xlsx" or "spreadsheetml.sheet" in mime_type:
        workbook = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
        lines = []
        for sheet in workbook.worksheets:
            lines.append(f"# Аркуш: {sheet.title}")
            for row in sheet.iter_rows(values_only=True):
                if any(cell is not None for cell in row):
                    lines.append(" | ".join("" if c is None else str(c) for c in row))
        return "\n".join(lines), None

    if ext == ".pptx" or "presentationml.presentation" in mime_type:
        presentation = Presentation(io.BytesIO(data))
        lines = []
        for i, slide in enumerate(presentation.slides, start=1):
            lines.append(f"# Слайд {i}")
            for shape in slide.shapes:
                if shape.has_text_frame:
                    text = shape.text_frame.text.strip()
                    if text:
                        lines.append(text)
        return "\n".join(lines), None

    text_exts = {".txt", ".md", ".csv", ".json", ".log", ".py", ".yaml", ".yml", ".xml", ".html"}
    if ext in text_exts or mime_type.startswith("text/"):
        return data.decode("utf-8", errors="ignore"), None

    return None, None


@dp.message(Command("start", "help"))
async def cmd_start(message: Message):
    await message.answer(
        "Привіт! Я бот-асистент. Я запам'ятовую переписку в чаті, "
        "а відповідаю, коли мене тегнуть (@бот) або звертаються по імені "
        f"({', '.join(TRIGGER_NAMES)})."
    )


@dp.message(Command("reset"))
async def cmd_reset(message: Message):
    history[message.chat.id].clear()
    await message.answer("Пам'ять цього чату очищена 🧹")


@dp.message(F.text)
async def handle_message(message: Message):
    me = await bot.get_me()
    bot_username = me.username

    chat_history = history[message.chat.id]
    sender = message.from_user.full_name if message.from_user else "Хтось"

    mentioned = was_mentioned(message, bot_username)

    if not mentioned:
        chat_history.append(
            {"role": "user", "parts": [{"text": f"{sender}: {message.text}"}]}
        )
        return

    question = strip_trigger(message.text, bot_username)
    if not question:
        question = "Привіт! Про що поговоримо?"

    full_prompt_text = f"{sender}: {question}"
    if needs_web_search(question):
        web_info = get_web_results(question)
        if web_info:
            full_prompt_text += web_info

    sender_context = get_sender_context(message)
    if sender_context:
        full_prompt_text += f"\n[Про співрозмовника: {sender_context}]"

    contents = list(chat_history)
    contents.append(
        {"role": "user", "parts": [{"text": full_prompt_text}]}
    )

    await bot.send_chat_action(message.chat.id, ChatAction.TYPING)

    try:
        answer = (await ask_gemini(contents)) or "Не зміг сформулювати відповідь 🤔"
        answer = strip_name_prefix(answer, sender, me.full_name)
    except Exception:
        log.exception("AI request failed")
        answer = "Вибач, сталася помилка при зверненні до AI 😔"

    chat_history.append({"role": "user", "parts": [{"text": f"{sender}: {question}"}]})
    chat_history.append({"role": "model", "parts": [{"text": answer}]})

    await message.reply(answer)


@dp.message(F.photo)
async def handle_photo(message: Message):
    me = await bot.get_me()
    bot_username = me.username

    chat_history = history[message.chat.id]
    sender = message.from_user.full_name if message.from_user else "Хтось"
    caption = message.caption or ""

    mentioned = was_mentioned(message, bot_username)

    if not mentioned:
        note = f"{sender}: [надіслав(-ла) фото]"
        if caption:
            note += f" {caption}"
        chat_history.append({"role": "user", "parts": [{"text": note}]})
        return

    question = strip_trigger(caption, bot_username) or "Що на цьому фото?"

    try:
        image_bytes = await download_photo_bytes(message)
    except Exception:
        log.exception("Failed to download photo")
        await message.reply("Не вдалось завантажити фото 😔")
        return

    await bot.send_chat_action(message.chat.id, ChatAction.TYPING)

    full_prompt_text = f"{sender}: {question}"
    if needs_web_search(question):
        web_info = get_web_results(question)
        if web_info:
            full_prompt_text += web_info

    sender_context = get_sender_context(message)
    if sender_context:
        full_prompt_text += f"\n[Про співрозмовника: {sender_context}]"

    contents = list(chat_history)
    contents.append(
        {
            "role": "user",
            "parts": [
                {"text": full_prompt_text},
                types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
            ],
        }
    )

    try:
        answer = (await ask_gemini(contents)) or "Не зміг сформулювати відповідь 🤔"
        answer = strip_name_prefix(answer, sender, me.full_name)
    except Exception:
        log.exception("AI request failed")
        answer = "Вибач, сталася помилка при зверненні до AI 😔"

    chat_history.append(
        {"role": "user", "parts": [{"text": f"{sender}: [фото] {question}"}]}
    )
    chat_history.append({"role": "model", "parts": [{"text": answer}]})

    await message.reply(answer)


async def download_voice_bytes(message: Message) -> bytes:
    """Завантажує аудіо голосового повідомлення (ogg/opus)."""
    voice = message.voice
    file = await bot.get_file(voice.file_id)
    buffer = await bot.download_file(file.file_path)
    return buffer.read()


@dp.message(F.voice)
async def handle_voice(message: Message):
    """Голосові повідомлення: Gemini сам транскрибує та розуміє аудіо напряму."""
    me = await bot.get_me()
    bot_username = me.username

    chat_history = history[message.chat.id]
    sender = message.from_user.full_name if message.from_user else "Хтось"
    caption = message.caption or ""

    mentioned = was_mentioned(message, bot_username)

    try:
        voice_bytes = await download_voice_bytes(message)
    except Exception:
        log.exception("Failed to download voice message")
        if mentioned:
            await message.reply("Не вдалось завантажити голосове 😔")
        return

    # Спочатку просимо Gemini просто транскрибувати аудіо в текст —
    # це потрібно і для запам'ятовування контексту, і для відповіді.
    transcript_contents = [
        {
            "role": "user",
            "parts": [
                {
                    "text": "Транскрибуй це голосове повідомлення дослівно, "
                    "тією мовою, якою його промовлено. У відповідь дай ТІЛЬКИ "
                    "текст транскрипції, без жодних коментарів чи лапок."
                },
                types.Part.from_bytes(data=voice_bytes, mime_type="audio/ogg"),
            ],
        }
    ]

    try:
        response = await ai_client.aio.models.generate_content(
            model=GEMINI_MODEL,
            contents=transcript_contents,
            config=types.GenerateContentConfig(max_output_tokens=400, temperature=0.2),
        )
        transcript = (response.text or "").strip()
    except Exception:
        log.exception("Voice transcription failed")
        transcript = ""

    if not transcript:
        if mentioned:
            await message.reply("Не вдалось розпізнати голосове повідомлення 😔")
        else:
            chat_history.append(
                {"role": "user", "parts": [{"text": f"{sender}: [голосове повідомлення]"}]}
            )
        return

    if not mentioned:
        note = f"{sender}: [голосове] {transcript}"
        if caption:
            note += f" ({caption})"
        chat_history.append({"role": "user", "parts": [{"text": note}]})
        return

    question = strip_trigger(caption, bot_username)
    full_prompt_text = f"{sender}: {transcript}"
    if question:
        full_prompt_text += f"\n[Коментар до голосового]: {question}"

    if needs_web_search(transcript):
        web_info = get_web_results(transcript)
        if web_info:
            full_prompt_text += web_info

    sender_context = get_sender_context(message)
    if sender_context:
        full_prompt_text += f"\n[Про співрозмовника: {sender_context}]"

    contents = list(chat_history)
    contents.append({"role": "user", "parts": [{"text": full_prompt_text}]})

    await bot.send_chat_action(message.chat.id, ChatAction.TYPING)

    try:
        answer = (await ask_gemini(contents)) or "Не зміг сформулювати відповідь 🤔"
        answer = strip_name_prefix(answer, sender, me.full_name)
    except Exception:
        log.exception("AI request failed")
        answer = "Вибач, сталася помилка при зверненні до AI 😔"

    chat_history.append(
        {"role": "user", "parts": [{"text": f"{sender}: [голосове] {transcript}"}]}
    )
    chat_history.append({"role": "model", "parts": [{"text": answer}]})

    await message.reply(answer)


@dp.message(F.document)
async def handle_document(message: Message):
    me = await bot.get_me()
    bot_username = me.username

    chat_history = history[message.chat.id]
    sender = message.from_user.full_name if message.from_user else "Хтось"
    caption = message.caption or ""
    doc = message.document
    file_name = doc.file_name or "файл"

    mentioned = was_mentioned(message, bot_username)

    if not mentioned:
        note = f"{sender}: [надіслав(-ла) файл {file_name}]"
        if caption:
            note += f" {caption}"
        chat_history.append({"role": "user", "parts": [{"text": note}]})
        return

    if doc.file_size and doc.file_size > 20 * 1024 * 1024:
        await message.reply("Файл більший за 20 МБ — стільки бот завантажити не може 😔")
        return

    try:
        tg_file = await bot.get_file(doc.file_id)
        buffer = await bot.download_file(tg_file.file_path)
        data = buffer.read()
    except Exception:
        log.exception("Failed to download document")
        await message.reply("Не вдалось завантажити файл 😔")
        return

    text_content, raw_part = extract_document_text(file_name, data, doc.mime_type)

    if text_content is None and raw_part is None:
        await message.reply(
            f"Не вмію читати такий формат ({file_name}). "
            "Підтримую PDF, DOCX, XLSX, PPTX і звичайні текстові файли "
            "(txt, csv, json, md тощо)."
        )
        return

    question = strip_trigger(caption, bot_username) or "Опрацюй цей файл і розкажи головне."

    full_prompt_text = f"{sender}: {question}\n\n[Файл: {file_name}]"
    if needs_web_search(question):
        web_info = get_web_results(question)
        if web_info:
            full_prompt_text += web_info

    sender_context = get_sender_context(message)
    if sender_context:
        full_prompt_text += f"\n[Про співрозмовника: {sender_context}]"

    parts = [{"text": full_prompt_text}]
    if raw_part is not None:
        parts.append(raw_part)
    else:
        trimmed = text_content[:MAX_DOC_CHARS]
        if len(text_content) > MAX_DOC_CHARS:
            trimmed += "\n...[текст обрізано, файл завеликий]"
        parts[0]["text"] += f"\n\nВміст файлу:\n{trimmed}"

    await bot.send_chat_action(message.chat.id, ChatAction.TYPING)

    contents = list(chat_history)
    contents.append({"role": "user", "parts": parts})

    try:
        answer = (await ask_gemini(contents)) or "Не зміг сформулювати відповідь 🤔"
        answer = strip_name_prefix(answer, sender, me.full_name)
    except Exception:
        log.exception("AI request failed")
        answer = "Вибач, сталася помилка при зверненні до AI 😔"

    chat_history.append(
        {"role": "user", "parts": [{"text": f"{sender}: [файл {file_name}] {question}"}]}
    )
    chat_history.append({"role": "model", "parts": [{"text": answer}]})

    await message.reply(answer)


async def main():
    print(f"Поточна модель в боті: {GEMINI_MODEL}")
    log.info("Бот запускається...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())