"""Все, що стосується обробки медіа-контенту: витяг тексту з документів,
завантаження файлів з Telegram, транскрибування аудіо/відео, побудова
контексту для reply на чуже медіа."""
import io
import logging
from pathlib import Path
import asyncio

import docx
import openpyxl
from aiogram import Bot
from aiogram.types import Message
from google.genai import types
from pptx import Presentation

log = logging.getLogger("Bashma4ek_Bot.media")

MAX_DOC_CHARS = 40000

async def wav_to_ogg_voice(wav_bytes: bytes) -> bytes | None:
    """Конвертує WAV у OGG/OPUS через ffmpeg, щоб Telegram показав відповідь
    як справжнє кругле голосове, а не файл-аудіо. Якщо ffmpeg відсутній на
    хості — повертає None, і хендлер підстрахується звичайним аудіофайлом."""
    try:
        process = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "wav", "-i", "-",
            "-c:a", "libopus", "-b:a", "32k", "-f", "ogg", "-",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate(input=wav_bytes)
        if process.returncode != 0 or not stdout:
            log.warning(f"ffmpeg не зміг сконвертувати аудіо: {stderr.decode(errors='ignore')[:300]}")
            return None
        return stdout
    except FileNotFoundError:
        log.info("ffmpeg не знайдено — голосова відповідь піде як звичайний аудіофайл")
        return None
    except Exception:
        log.exception("Помилка при конвертації WAV → OGG")
        return None


async def download_telegram_file(bot: Bot, file_id: str) -> bytes | None:
    """Спільна логіка завантаження файлу з Telegram — використовується
    в усіх медіа-хендлерах замість повторення однакового try/except."""
    try:
        file = await bot.get_file(file_id)
        buffer = await bot.download_file(file.file_path)
        return buffer.read()
    except Exception:
        log.exception(f"Не вдалось завантажити файл {file_id}")
        return None


def extract_document_text(file_name: str, data: bytes, mime_type: str | None):
    """Готує вміст файлу для Gemini. Повертає (text_content, raw_part) —
    рівно одне з двох не None (raw_part для форматів, які Gemini читає
    напряму типу PDF, text_content для решти)."""
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


def trim_document_text(text_content: str) -> str:
    """Обрізає текст документа до MAX_DOC_CHARS з поміткою про обрізання."""
    trimmed = text_content[:MAX_DOC_CHARS]
    if len(text_content) > MAX_DOC_CHARS:
        trimmed += "\n...[текст обрізано, файл завеликий]"
    return trimmed


async def build_reply_media_context(
    bot: Bot, replied: Message, transcribe_media,
) -> tuple[list, str]:
    """Якщо повідомлення, на яке відповіли (reply), містить фото/стікер/
    гіфку/файл/голосове/кружок — підвантажує цей вміст і повертає
    (extra_parts, опис для промпту). Завдяки цьому 'reply на фото + тег
    бота в тексті' сприймається так само, ніби фото щойно надіслали й
    одразу тегнули бота під ним.

    transcribe_media передається ззовні (з gemini_client), щоб уникнути
    циклічного імпорту media_utils <-> gemini_client.
    """
    extra_parts: list = []
    description = ""

    try:
        if replied.photo:
            data = await download_telegram_file(bot, replied.photo[-1].file_id)
            if data:
                extra_parts.append(types.Part.from_bytes(data=data, mime_type="image/jpeg"))
                description = "[у відповідь на фото]"
                if replied.caption:
                    description += f" (підпис до фото: {replied.caption})"

        elif replied.sticker and not (replied.sticker.is_animated or replied.sticker.is_video):
            data = await download_telegram_file(bot, replied.sticker.file_id)
            if data:
                extra_parts.append(types.Part.from_bytes(data=data, mime_type="image/webp"))
                description = f"[у відповідь на стікер {replied.sticker.emoji or ''}]"

        elif replied.sticker:
            # анімовані/відео-стікери vision не читає — тільки емодзі
            description = f"[у відповідь на анімований стікер {replied.sticker.emoji or ''}]"

        elif replied.animation:
            data = await download_telegram_file(bot, replied.animation.file_id)
            if data:
                extra_parts.append(types.Part.from_bytes(data=data, mime_type="video/mp4"))
                description = "[у відповідь на гіфку]"
                if replied.caption:
                    description += f" (підпис до гіфки: {replied.caption})"

        elif replied.document:
            file_name = replied.document.file_name or "файл"
            data = await download_telegram_file(bot, replied.document.file_id)
            if data:
                text_content, raw_part = extract_document_text(
                    file_name, data, replied.document.mime_type
                )
                description = f"[у відповідь на файл {file_name}]"
                if raw_part is not None:
                    extra_parts.append(raw_part)
                elif text_content:
                    description += f"\nВміст файлу:\n{trim_document_text(text_content)}"
                else:
                    description += " (формат файлу не підтримується)"

        elif replied.voice:
            data = await download_telegram_file(bot, replied.voice.file_id)
            if data:
                transcript = await transcribe_media(data, "audio/ogg", "voice-reply")
                if transcript:
                    description = f"[у відповідь на голосове]: {transcript}"

        elif replied.video_note:
            data = await download_telegram_file(bot, replied.video_note.file_id)
            if data:
                transcript = await transcribe_media(data, "video/mp4", "video_note-reply")
                if transcript:
                    description = f"[у відповідь на кружок]: {transcript}"

    except Exception:
        log.exception("Не вдалось підвантажити медіа з reply_to_message")
        return [], ""

    return extra_parts, description
