"""Хендлери Telegram-повідомлень і команд. Кожен хендлер робить по суті
одне й те саме: сформувати текстовий опис події, за потреби додати
веб-контекст і контекст про відправника, звернутись до Gemini і записати
результат в історію чату. Різниця лише в тому, як саме будується
"сирий" контент (question_text) і які додаткові Part-и (фото/відео/файл)
додаються в запит."""
import asyncio
import logging
import random
import re
import time

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatAction
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, Message, ReactionTypeEmoji
from google.genai import types

import bot_state
from config import GEMINI_MODEL, GEMINI_RPD_LIMIT, TRIGGER_NAMES, USER_CONTEXT
from gemini_client import (
    ask_gemini,
    check_reaction_worthy,
    gemini_stats,
    parse_reaction_answer,
    quota_low,
    transcribe_media,
)
from media_utils import (
    build_reply_media_context,
    download_telegram_file,
    extract_document_text,
    trim_document_text,
)
from search_utils import (
    get_current_date_str,
    get_web_context,
    is_image_query,
    is_weekend,
    needs_web_search,
)

log = logging.getLogger("Bashma4ek_Bot.handlers")

# --- Реакції на повідомлення без тегу бота ----------------------------------
REACT_UNPROMPTED_ENABLED_DEFAULT = True
import os
REACT_UNPROMPTED_ENABLED = os.getenv("REACT_UNPROMPTED_ENABLED", "true").lower() == "true"
REACT_UNPROMPTED_CHANCE = float(os.getenv("REACT_UNPROMPTED_CHANCE", "0.35"))

IMAGE_MIME_JPEG = "image/jpeg"

_NAMES_ALT = "|".join(re.escape(n) for n in TRIGGER_NAMES)
NAME_PATTERN = re.compile(
    rf"(^\s*({_NAMES_ALT})\b)|(\b({_NAMES_ALT})\s*[,!?.]*\s*$)",
    re.IGNORECASE,
) if TRIGGER_NAMES else None


def strip_trigger(text: str, bot_username: str) -> str:
    """Прибирає @згадку бота та тригер-ім'я з тексту питання."""
    text = text or ""
    text = text.replace(f"@{bot_username}", "")
    if NAME_PATTERN:
        text = NAME_PATTERN.sub("", text, count=1)
    return text.strip(" ,:.!?-")


def strip_name_prefix(text: str, sender: str, bot_name: str) -> str:
    """Прибирає префікс на кшталт 'koserpa: ' або 'Башмак: ' з відповіді моделі."""
    text = text.strip()
    names = "|".join(re.escape(n) for n in {sender, bot_name, *TRIGGER_NAMES} if n)
    if not names:
        return text
    text = re.sub(rf"^\s*(?:{names})\s*:\s*", "", text, count=1, flags=re.IGNORECASE)
    return text.strip()


def was_mentioned(message: Message) -> bool:
    text = message.text or message.caption

    if message.reply_to_message and message.reply_to_message.from_user:
        if message.reply_to_message.from_user.id == bot_state.BOT_ID:
            return True

    if not text:
        return False

    if bot_state.BOT_USERNAME and f"@{bot_state.BOT_USERNAME}".lower() in text.lower():
        return True

    if NAME_PATTERN and NAME_PATTERN.search(text):
        return True

    return False


def get_sender_context(message: Message) -> str:
    """Повертає підказку боту про відправника, якщо це відомий учасник."""
    if not message.from_user or not message.from_user.username:
        return ""
    username = message.from_user.username.lstrip("@").lower()
    for known_username, context in USER_CONTEXT.items():
        if known_username.lower() == username:
            return context
    return ""


def get_mentioned_users_context(text: str) -> str:
    """Шукає в тексті @згадки відомих учасників (окрім самого бота) і
    повертає для них контекст із USER_CONTEXT."""
    if not text:
        return ""

    mentioned_usernames = set(re.findall(r"@(\w+)", text))
    bot_username_lower = (bot_state.BOT_USERNAME or "").lower()

    blocks = []
    for username in mentioned_usernames:
        if username.lower() == bot_username_lower:
            continue
        for known_username, context in USER_CONTEXT.items():
            if known_username.lower() == username.lower():
                blocks.append(f"@{known_username}: {context}")
                break

    if not blocks:
        return ""
    return "\n[Про згаданих людей]:\n" + "\n".join(blocks)


async def _try_send_image_url(bot: Bot, chat_id: int, url: str) -> bool:
    """Качає картинку сама і перевіряє, що це валідне зображення, перш ніж
    слати в Telegram — деякі URL повертають 404 або HTML-заглушку замість
    картинки, і send_photo(url) не завжди це ловить."""
    import requests
    try:
        resp = await asyncio.to_thread(
            requests.get,
            url,
            timeout=8,
            headers={"User-Agent": "Mozilla/5.0 (compatible; BashmakBot/1.0)"},
        )
        resp.raise_for_status()
        content_type = resp.headers.get("Content-Type", "")
        if not content_type.startswith("image/"):
            log.warning(f"Пропускаю картинку {url}: content-type={content_type!r}")
            return False
        data = resp.content
        if len(data) < 500:
            log.warning(f"Пропускаю картинку {url}: підозріло малий розмір ({len(data)} байт)")
            return False
    except Exception as e:
        log.warning(f"Не вдалось завантажити картинку {url}: {e}")
        return False

    try:
        await bot.send_photo(chat_id, BufferedInputFile(data, filename="image.jpg"))
        return True
    except Exception:
        log.exception(f"Не вдалось надіслати картинку {url}")
        return False


async def maybe_react_unprompted(bot: Bot, message: Message, sender: str, content_text: str) -> None:
    """Може поставити емодзі-реакцію на будь-яке повідомлення, навіть якщо
    бота не тегали — вибірково і лише коли це реально доречно."""
    if not REACT_UNPROMPTED_ENABLED:
        return
    if not content_text or not content_text.strip():
        return
    if quota_low():
        return
    if random.random() > REACT_UNPROMPTED_CHANCE:
        return

    emoji = await check_reaction_worthy(sender, content_text)
    if not emoji:
        return

    try:
        await bot.set_message_reaction(
            chat_id=message.chat.id,
            message_id=message.message_id,
            reaction=[ReactionTypeEmoji(emoji=emoji)],
        )
    except Exception:
        log.exception("Не вдалось поставити незапитану реакцію")


def remember_only(bot: Bot, message: Message, sender: str, note: str) -> None:
    """Записує подію в історію чату без звернення до Gemini (коли бота не
    згадали). Додатково запускає фонову перевірку — чи не варто все ж
    відреагувати емодзі на це повідомлення (без тегу)."""
    bot_state.history[message.chat.id].append(
        {"role": "user", "parts": [{"text": f"{sender}: {note}"}]}
    )
    asyncio.create_task(maybe_react_unprompted(bot, message, sender, note))


async def process_and_reply(
    bot: Bot,
    message: Message,
    sender: str,
    question_text: str,
    *,
    extra_parts: list | None = None,
    history_label: str,
) -> None:
    chat_history = bot_state.history[message.chat.id]

    full_prompt_text = f"{sender}: {question_text}"
    image_urls: list[str] = []
    if needs_web_search(question_text) or is_image_query(question_text):
        web_info, image_urls = await get_web_context(question_text)
        if web_info:
            full_prompt_text += web_info

    sender_context = get_sender_context(message)
    if sender_context:
        full_prompt_text += f"\n[Про співрозмовника: {sender_context}]"

    mentioned_context = get_mentioned_users_context(question_text)
    if mentioned_context:
        full_prompt_text += mentioned_context

    parts = [{"text": full_prompt_text}]
    if extra_parts:
        parts.extend(extra_parts)

    contents = list(chat_history)
    contents.append({"role": "user", "parts": parts})

    await bot.send_chat_action(message.chat.id, ChatAction.TYPING)

    try:
        answer = (await ask_gemini(contents, get_current_date_str())) or "Не зміг сформулювати відповідь 🤔"
        answer = strip_name_prefix(answer, sender, bot_state.BOT_FULL_NAME)
    except Exception:
        log.exception("AI request failed")
        answer = "Вибач, сталася помилка при зверненні до AI 😔"

    chat_history.append(
        {"role": "user", "parts": [{"text": f"{sender}: {history_label}"}]}
    )

    # Якщо вже знайдені картинки для відповіді — ігноруємо маркер REACTION,
    # інакше модель могла б поставити емодзі й "з'їсти" знайдені картинки.
    reaction_emoji = parse_reaction_answer(answer) if not image_urls else None

    if reaction_emoji:
        chat_history.append(
            {"role": "model", "parts": [{"text": f"(відреагував {reaction_emoji})"}]}
        )
        try:
            await bot.set_message_reaction(
                chat_id=message.chat.id,
                message_id=message.message_id,
                reaction=[ReactionTypeEmoji(emoji=reaction_emoji)],
            )
        except Exception:
            log.exception("Не вдалось поставити реакцію, відповідаю текстом")
            await message.reply(reaction_emoji)
    else:
        chat_history.append({"role": "model", "parts": [{"text": answer}]})
        await message.reply(answer)

    for url in image_urls:
        await _try_send_image_url(bot, message.chat.id, url)


async def send_idle_message(bot: Bot, chat_id: int) -> None:
    """Формує і надсилає одне проактивне повідомлення в тихий чат."""
    from config import HISTORY_SIZE  # noqa: F401  (лишено для явності залежності)

    chat_history = bot_state.history[chat_id]
    contents = list(chat_history)
    idle_hours = float(os.getenv("IDLE_HOURS", "7"))
    contents.append(
        {
            "role": "user",
            "parts": [{
                "text": (
                    f"[СИСТЕМНЕ]: у чаті тиша вже {idle_hours:.0f}+ годин. "
                    "Напиши щось одне коротке від себе, щоб оживити чат — "
                    "жарт, провокаційне питання чи коротку думку, за темою "
                    "останніх повідомлень якщо вони були, у своєму "
                    "звичному стилі. Рівно 1 речення. Без звернення до "
                    "когось конкретного і без пояснень, що ти бот, що чат "
                    "мовчав, чи щось подібне — просто природне повідомлення."
                )
            }],
        }
    )

    answer = await ask_gemini(contents, get_current_date_str())
    if not answer or parse_reaction_answer(answer):
        return

    answer = strip_name_prefix(answer, "", bot_state.BOT_FULL_NAME)
    chat_history.append({"role": "model", "parts": [{"text": answer}]})
    await bot.send_message(chat_id, answer)


async def idle_chat_watcher(bot: Bot):
    """Раз на IDLE_CHECK_INTERVAL_SEC проходиться по відомих чатах: якщо
    тиша довша за IDLE_HOURS і бот ще не писав за цей період тиші —
    надсилає одне проактивне повідомлення. У суботу та неділю проактивні
    повідомлення вимкнено."""
    idle_hours = float(os.getenv("IDLE_HOURS", "7"))
    idle_check_interval_sec = 15 * 60

    while True:
        await asyncio.sleep(idle_check_interval_sec)
        now = time.time()

        if is_weekend():
            continue  # вихідні — бот сам не пише

        for chat_id, last_active in list(bot_state.last_human_activity.items()):
            if bot_state.idle_message_sent.get(chat_id):
                continue
            if now - last_active < idle_hours * 3600:
                continue

            bot_state.idle_message_sent[chat_id] = True
            try:
                await send_idle_message(bot, chat_id)
            except Exception:
                log.exception(f"Не вдалось надіслати проактивне повідомлення в чат {chat_id}")

def register_handlers(dp: Dispatcher, bot: Bot) -> None:
    """Реєструє всі хендлери в переданому Dispatcher. Bot передається явно
    (замість глобального імпорту), щоб handlers.py не залежав від того, де
    саме створюється інстанс Bot."""

    @dp.message.outer_middleware()
    async def track_activity_middleware(handler, message: Message, data: dict):
        """Фіксує будь-яке повідомлення від людини в чаті."""
        if message.chat.type != "private":
            bot_state.last_human_activity[message.chat.id] = time.time()
            bot_state.idle_message_sent[message.chat.id] = False
        return await handler(message, data)

    @dp.message(Command("start", "help"))
    async def cmd_start(message: Message):
        await message.answer(
            "Привіт! Я бот-асистент. Я запам'ятовую переписку в чаті, "
            "а відповідаю, коли мене тегнуть (@бот) або звертаються по імені "
            f"({', '.join(TRIGGER_NAMES)})."
        )

    @dp.message(Command("reset"))
    async def cmd_reset(message: Message):
        bot_state.history[message.chat.id].clear()
        await message.answer("Пам'ять цього чату очищена 🧹")

    @dp.message(Command("status"))
    async def cmd_status(message: Message):
        from config import HISTORY_SIZE

        uptime_sec = int(time.time() - bot_state.START_TIME)
        hours, remainder = divmod(uptime_sec, 3600)
        minutes, seconds = divmod(remainder, 60)
        chat_len = len(bot_state.history[message.chat.id])

        await message.answer(
            "📊 Статус бота\n"
            f"Модель: {GEMINI_MODEL}\n"
            f"Uptime: {hours}г {minutes}хв {seconds}с\n"
            f"Повідомлень в пам'яті цього чату: {chat_len}/{HISTORY_SIZE}"
        )

    @dp.message(Command("model"))
    async def cmd_model(message: Message):
        lines = [
            f"🤖 Модель: <code>{GEMINI_MODEL}</code>",
            f"📊 Запитів сьогодні: {gemini_stats.count_today}",
            f"📈 Запитів з моменту останнього рестарту: {gemini_stats.count_total}",
        ]
        if GEMINI_RPD_LIMIT:
            remaining = max(GEMINI_RPD_LIMIT - gemini_stats.count_today, 0)
            lines.append(f"🎯 Ліміт RPD: {GEMINI_RPD_LIMIT} (залишилось ~{remaining})")
        else:
            lines.append("🎯 Ліміт RPD не задано в конфігу (GEMINI_RPD_LIMIT)")

        await message.answer("\n".join(lines))

    @dp.message(F.text)
    async def handle_message(message: Message):
        sender = message.from_user.full_name if message.from_user else "Хтось"
        mentioned = was_mentioned(message)

        if not mentioned:
            remember_only(bot, message, sender, message.text)
            return

        question = strip_trigger(message.text, bot_state.BOT_USERNAME)
        if not question:
            question = "Привіт! Про що поговоримо?"

        extra_parts: list = []
        replied = message.reply_to_message
        if replied and (not replied.from_user or replied.from_user.id != bot_state.BOT_ID):
            reply_parts, reply_description = await build_reply_media_context(
                bot, replied, transcribe_media
            )
            if reply_description:
                question = f"{question}\n{reply_description}" if question else reply_description
                extra_parts = reply_parts

        await process_and_reply(
            bot, message, sender, question,
            extra_parts=extra_parts or None,
            history_label=question,
        )

    @dp.message(F.photo)
    async def handle_photo(message: Message):
        sender = message.from_user.full_name if message.from_user else "Хтось"
        caption = message.caption or ""
        mentioned = was_mentioned(message)

        if not mentioned:
            note = "[надіслав(-ла) фото]" + (f" {caption}" if caption else "")
            remember_only(bot, message, sender, note)
            return

        question = strip_trigger(caption, bot_state.BOT_USERNAME) or "Що на цьому фото?"
        image_bytes = await download_telegram_file(bot, message.photo[-1].file_id)
        if image_bytes is None:
            await message.reply("Не вдалось завантажити фото 😔")
            return

        extra_parts = [types.Part.from_bytes(data=image_bytes, mime_type=IMAGE_MIME_JPEG)]
        await process_and_reply(
            bot, message, sender, question,
            extra_parts=extra_parts,
            history_label=f"[фото] {question}",
        )

    @dp.message(F.sticker)
    async def handle_sticker(message: Message):
        sender = message.from_user.full_name if message.from_user else "Хтось"
        sticker = message.sticker
        emoji = sticker.emoji or "🙂"
        mentioned = was_mentioned(message)

        # анімовані (.tgs) і відео-стікери (.webm) Gemini vision напряму не
        # їсть — фіксуємо тільки емодзі, без реального аналізу картинки.
        if sticker.is_animated or sticker.is_video:
            if not mentioned:
                remember_only(bot, message, sender, f"[анімований стікер {emoji}]")
                return
            await process_and_reply(
                bot, message, sender, f"[надіслав(-ла) анімований стікер {emoji}]",
                history_label=f"[анімований стікер {emoji}]",
            )
            return

        if not mentioned:
            remember_only(bot, message, sender, f"[надіслав(-ла) стікер {emoji}]")
            return

        sticker_bytes = await download_telegram_file(bot, sticker.file_id)
        if sticker_bytes is None:
            await message.reply("Не вдалось завантажити стікер 😔")
            return

        extra_parts = [types.Part.from_bytes(data=sticker_bytes, mime_type="image/webp")]
        await process_and_reply(
            bot, message, sender, f"[надіслав(-ла) стікер, емодзі: {emoji}]",
            extra_parts=extra_parts,
            history_label=f"[надіслав(-ла) стікер {emoji}]",
        )

    @dp.message(F.animation)
    async def handle_animation(message: Message):
        """GIF в Telegram технічно приходить як mp4 без звуку (F.animation)."""
        sender = message.from_user.full_name if message.from_user else "Хтось"
        caption = message.caption or ""
        mentioned = was_mentioned(message)

        if not mentioned:
            note = "[надіслав(-ла) гіфку]" + (f" {caption}" if caption else "")
            remember_only(bot, message, sender, note)
            return

        animation = message.animation
        if animation.file_size and animation.file_size > 20 * 1024 * 1024:
            await message.reply("Гіфка більша за 20 МБ — стільки бот завантажити не може 😔")
            return

        animation_bytes = await download_telegram_file(bot, animation.file_id)
        if animation_bytes is None:
            await message.reply("Не вдалось завантажити гіфку 😔")
            return

        question = strip_trigger(caption, bot_state.BOT_USERNAME) or "Що відбувається на цій гіфці?"
        extra_parts = [types.Part.from_bytes(data=animation_bytes, mime_type="video/mp4")]
        await process_and_reply(
            bot, message, sender, question,
            extra_parts=extra_parts,
            history_label=f"[гіфка] {question}",
        )

    @dp.message(F.video_note)
    async def handle_video_note(message: Message):
        """Кружки: транскрибуємо мовлення так само, як голосові."""
        sender = message.from_user.full_name if message.from_user else "Хтось"
        mentioned = was_mentioned(message)

        video_note_bytes = await download_telegram_file(bot, message.video_note.file_id)
        if video_note_bytes is None:
            if mentioned:
                await message.reply("Не вдалось завантажити кружок 😔")
            return

        transcript = await transcribe_media(video_note_bytes, "video/mp4", "video_note")

        if not transcript:
            if mentioned:
                await message.reply("Не вдалось розпізнати кружок 😔")
            else:
                remember_only(bot, message, sender, "[кружок]")
            return

        if not mentioned:
            remember_only(bot, message, sender, f"[кружок] {transcript}")
            return

        await process_and_reply(
            bot, message, sender, transcript,
            history_label=f"[кружок] {transcript}",
        )

    @dp.message(F.voice)
    async def handle_voice(message: Message):
        """Голосові повідомлення: Gemini сам транскрибує та розуміє аудіо."""
        sender = message.from_user.full_name if message.from_user else "Хтось"
        caption = message.caption or ""
        mentioned = was_mentioned(message)

        voice_bytes = await download_telegram_file(bot, message.voice.file_id)
        if voice_bytes is None:
            if mentioned:
                await message.reply("Не вдалось завантажити голосове 😔")
            return

        transcript = await transcribe_media(voice_bytes, "audio/ogg", "voice")

        if not transcript:
            if mentioned:
                await message.reply("Не вдалось розпізнати голосове повідомлення 😔")
            else:
                remember_only(bot, message, sender, "[голосове повідомлення]")
            return

        if not mentioned:
            note = f"[голосове] {transcript}" + (f" ({caption})" if caption else "")
            remember_only(bot, message, sender, note)
            return

        question = strip_trigger(caption, bot_state.BOT_USERNAME)
        question_text = transcript
        if question:
            question_text += f"\n[Коментар до голосового]: {question}"

        await process_and_reply(
            bot, message, sender, question_text,
            history_label=f"[голосове] {transcript}",
        )

    @dp.message(F.document)
    async def handle_document(message: Message):
        sender = message.from_user.full_name if message.from_user else "Хтось"
        caption = message.caption or ""
        doc = message.document
        file_name = doc.file_name or "файл"
        mentioned = was_mentioned(message)

        if not mentioned:
            note = f"[надіслав(-ла) файл {file_name}]" + (f" {caption}" if caption else "")
            remember_only(bot, message, sender, note)
            return

        if doc.file_size and doc.file_size > 20 * 1024 * 1024:
            await message.reply("Файл більший за 20 МБ — стільки бот завантажити не може 😔")
            return

        data = await download_telegram_file(bot, doc.file_id)
        if data is None:
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

        question = strip_trigger(caption, bot_state.BOT_USERNAME) or "Опрацюй цей файл і розкажи головне."
        question_text = f"{question}\n\n[Файл: {file_name}]"

        extra_parts = None
        if raw_part is not None:
            extra_parts = [raw_part]
        else:
            question_text += f"\n\nВміст файлу:\n{trim_document_text(text_content)}"

        await process_and_reply(
            bot, message, sender, question_text,
            extra_parts=extra_parts,
            history_label=f"[файл {file_name}] {question}",
        )
