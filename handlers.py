"""Хендлери Telegram-повідомлень і команд. Кожен хендлер робить по суті
одне й те саме: сформувати текстовий опис події, за потреби додати
веб-контекст і контекст про відправника, звернутись до Gemini і записати
результат в історію чату. Різниця лише в тому, як саме будується
"сирий" контент (question_text) і які додаткові Part-и (фото/відео/файл)
додаються в запит."""
import asyncio
import logging
import os
import random
import time
from typing import Awaitable, Callable

import requests
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatAction
from aiogram.filters import BaseFilter, Command
from aiogram.types import BufferedInputFile, Message, ReactionTypeEmoji
from google.genai import types

import absence_utils
import bot_state
import drama_utils
from config import ADMIN_USERNAME, GEMINI_MODEL, GEMINI_RPD_LIMIT, TRIGGER_NAMES, USER_CONTEXT
from features import (
    REACT_UNPROMPTED_CHANCE,
    REACT_UNPROMPTED_ENABLED,
    VOICE_REPLY_ENABLED,
    describe_flags,
)
from gemini_client import (
    ask_gemini,
    check_reaction_worthy,
    gemini_stats,
    parse_reaction_answer,
    parse_voice_marker,
    quota_low,
    synthesize_speech,
    transcribe_media,
    tts_quota_low,
)
from media_utils import (
    build_reply_media_context,
    download_or_reply,
    download_telegram_file,
    extract_document_text,
    trim_document_text,
    wav_to_ogg_voice,
)
from search_utils import (
    get_current_date_str,
    get_web_context,
    is_image_query,
    is_weekend,
    needs_web_search,
)
from text_utils import (
    get_mentioned_usernames,
    strip_name_prefix,
    strip_trigger,
    text_mentions_bot_username,
    text_mentions_trigger_name,
    wants_forced_voice,
)

log = logging.getLogger("Bashma4ek_Bot.handlers")


class IsAdmin(BaseFilter):
    async def __call__(self, message: Message) -> bool:
        username = (message.from_user.username or "").lstrip("@").lower() if message.from_user else ""
        return username == ADMIN_USERNAME


IMAGE_MIME_JPEG = "image/jpeg"


# --- Фонові таски (drama/absence/unprompted reactions) ----------------------
# create_task() саме по собі не тримає посилання на об'єкт — теоретично GC
# може прибрати таску до завершення. Тримаємо явний set() і одразу логуємо
# будь-який виняток, який інакше мовчки загубився б у "fire-and-forget" виклику.
_background_tasks: set[asyncio.Task] = set()


def spawn_background(coro) -> None:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)

    def _on_done(t: asyncio.Task) -> None:
        _background_tasks.discard(t)
        if not t.cancelled() and t.exception() is not None:
            log.error("Фонова таска впала з помилкою", exc_info=t.exception())

    task.add_done_callback(_on_done)


def was_mentioned(message: Message) -> bool:
    text = message.text or message.caption

    if message.reply_to_message and message.reply_to_message.from_user:
        if message.reply_to_message.from_user.id == bot_state.BOT_ID:
            return True

    if not text:
        return False

    return (
        text_mentions_bot_username(text, bot_state.BOT_USERNAME)
        or text_mentions_trigger_name(text)
    )


def get_sender_context(message: Message) -> str:
    if not message.from_user or not message.from_user.username:
        return ""
    username = message.from_user.username.lstrip("@").lower()
    for known_username, ctx in USER_CONTEXT.items():
        if known_username.lower() == username:
            if isinstance(ctx, dict):
                text = ctx.get("style", "")
                if ctx.get("sensitive"):
                    text += (
                        f"\n[Чутливе, НЕ піднімай як тему; використовуй лише "
                        f"якщо людина сама зачепить це: {ctx['sensitive']}]"
                    )
                return text
            return ctx  # backward-compat for plain-string entries
    return ""


def get_full_context_raw(message: Message) -> str:
    """Повний контекст (style + sensitive) без обмежень — для приколів типу /gadalka."""
    if not message.from_user or not message.from_user.username:
        return ""
    return get_full_context_raw_by_username(message.from_user.username)


def get_full_context_raw_by_username(username: str) -> str:
    """Те саме що get_full_context_raw, але за голим username (коли Message
    людини під рукою немає — напр. для absence-детектора)."""
    for known_username, ctx in USER_CONTEXT.items():
        if known_username.lower() == (username or "").lower():
            if isinstance(ctx, dict):
                parts = [ctx.get("style", "")]
                if ctx.get("sensitive"):
                    parts.append(ctx["sensitive"])
                return " ".join(p for p in parts if p)
            return ctx
    return ""


def get_mentioned_users_context(text: str) -> str:
    """Шукає в тексті @згадки відомих учасників (окрім самого бота) і
    повертає для них контекст із USER_CONTEXT."""
    if not text:
        return ""

    mentioned_usernames = get_mentioned_usernames(text)
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


async def _send_voice_reply(bot: Bot, chat_id: int, text: str, reply_to_message_id: int) -> bool:
    """Повертає True, якщо голосове реально пішло — щоб виклик міг
    вирішити, чи потрібен ще й текстовий фолбек."""
    if quota_low():
        return False
    wav_bytes = await synthesize_speech(text)
    if not wav_bytes:
        return False
    ogg_bytes = await wav_to_ogg_voice(wav_bytes)
    try:
        if ogg_bytes:
            await bot.send_voice(
                chat_id, BufferedInputFile(ogg_bytes, filename="voice.ogg"),
                reply_to_message_id=reply_to_message_id,
            )
        else:
            await bot.send_audio(
                chat_id, BufferedInputFile(wav_bytes, filename="voice.wav"),
                reply_to_message_id=reply_to_message_id,
            )
        return True
    except Exception:
        log.exception("Не вдалось надіслати голосову відповідь")
        return False


async def _try_send_image_url(bot: Bot, chat_id: int, url: str) -> bool:
    """Качає картинку сама і перевіряє, що це валідне зображення, перш ніж
    слати в Telegram — деякі URL повертають 404 або HTML-заглушку замість
    картинки, і send_photo(url) не завжди це ловить."""
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


async def maybe_intervene_drama(bot: Bot, message: Message) -> None:
    """Якщо детектор побачив 'срач' у чаті — генерує одне коротке
    втручання в стилі бота (не модераторський тон)."""
    chat_id = message.chat.id
    if not drama_utils.is_drama_happening(chat_id):
        return
    if quota_low():
        return

    drama_utils.mark_drama_handled(chat_id)  # одразу, щоб не задвоїти

    recent_history = list(bot_state.history[chat_id])[-12:]
    prompt = (
        "У чаті зараз накал — люди сваряться/зʼясовують стосунки. "
        "Встрянь ОДНИМ коротким реченням у своєму звичному стилі: або "
        "розряди обстановку жартом, або підколи всіх одразу, не вибираючи "
        "сторону. НЕ повчай і не проси прямим текстом типу 'заспокойтесь' "
        "— має звучати як природна репліка живої людини в чаті, а не "
        "модератора."
    )
    contents = recent_history + [{"role": "user", "parts": [{"text": prompt}]}]

    try:
        answer = await ask_gemini(contents, get_current_date_str())
    except Exception:
        log.exception("Не вдалось згенерувати drama-коментар")
        return

    if not answer or parse_reaction_answer(answer):
        return

    answer = strip_name_prefix(answer, "", bot_state.BOT_FULL_NAME)
    bot_state.history[chat_id].append({"role": "model", "parts": [{"text": answer}]})
    try:
        await bot.send_message(chat_id, answer)
    except Exception:
        log.exception(f"Не вдалось надіслати drama-коментар в чат {chat_id}")


async def maybe_poke_absent_user(bot: Bot, message: Message) -> None:
    """Якщо хтось відомий довго мовчить на фоні активного чату — іноді
    підколює його відсутність."""
    chat_id = message.chat.id
    current_user_id = message.from_user.id if message.from_user else 0

    candidate = absence_utils.find_absent_candidate(chat_id, current_user_id)
    if not candidate:
        return
    if not absence_utils.should_roll_poke():
        return
    if quota_low():
        return

    user_id, full_name, username, silence_hours = candidate
    absence_utils.mark_poked(chat_id, user_id)  # одразу, щоб не задвоїти

    days = silence_hours / 24
    style = get_full_context_raw_by_username(username)
    prompt = (
        f"{full_name} не писав(-ла) в чаті вже приблизно {days:.1f} дні. "
        f"Хтось щойно написав у чаті — на фоні цього встав ОДНЕ коротке "
        f"речення у своєму стилі, де підколюєш відсутність {full_name} "
        f"(наприклад, як в стилі 'де {full_name.split()[0]}?'), спираючись "
        f"на його профіль нижче, якщо доречно. Без пояснень, що ти бот.\n"
        f"[Про {full_name}]: {style}"
    )

    contents = list(bot_state.history[chat_id])[-10:] + [
        {"role": "user", "parts": [{"text": prompt}]}
    ]

    try:
        answer = await ask_gemini(contents, get_current_date_str())
    except Exception:
        log.exception("Не вдалось згенерувати absence-підкол")
        return

    if not answer or parse_reaction_answer(answer):
        return

    answer = strip_name_prefix(answer, "", bot_state.BOT_FULL_NAME)
    bot_state.history[chat_id].append({"role": "model", "parts": [{"text": answer}]})
    try:
        await bot.send_message(chat_id, answer)
    except Exception:
        log.exception(f"Не вдалось надіслати absence-підкол в чат {chat_id}")


def remember_only(bot: Bot, message: Message, sender: str, note: str) -> None:
    """Записує подію в історію чату без звернення до Gemini (коли бота не
    згадали). Додатково запускає фонову перевірку — чи не варто все ж
    відреагувати емодзі на це повідомлення (без тегу)."""
    bot_state.history[message.chat.id].append(
        {"role": "user", "parts": [{"text": f"{sender}: {note}"}]}
    )
    spawn_background(maybe_react_unprompted(bot, message, sender, note))


async def process_and_reply(
    bot: Bot, message: Message, sender: str, question_text: str, *,
    extra_parts: list | None = None, history_label: str | None = None,
    is_voice_input: bool = False,
) -> None:
    history_label = history_label if history_label is not None else question_text
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

    model_wants_voice, answer = parse_voice_marker(answer)
    force_voice = wants_forced_voice(question_text)
    should_send_voice = (
        (is_voice_input or model_wants_voice or force_voice)
        and not tts_quota_low()
    )

    chat_history.append(
        {"role": "user", "parts": [{"text": f"{sender}: {history_label}"}]}
    )

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
        voice_sent = False
        if should_send_voice:
            voice_sent = await _send_voice_reply(bot, message.chat.id, answer, message.message_id)
        if not voice_sent:
            await message.reply(answer)

    for url in image_urls:
        await _try_send_image_url(bot, message.chat.id, url)


# --- Спільний диспетчер для медіа-хендлерів ---------------------------------
# Photo / sticker / animation / document мають однаковий каркас: якщо бота
# не тегнули — просто запам'ятати подію й вийти; якщо тегнули — підготувати
# (question_text, extra_parts, history_label) і піти в process_and_reply.
# Voice / video_note свідомо тут не використовуються — транскрипція там
# потрібна ДО перевірки was_mentioned (бо йде і в repam'ятовування теж).
PrepareResult = tuple[str, list | None, str] | None
PrepareFn = Callable[[], Awaitable[PrepareResult]]


async def dispatch_media_event(
    bot: Bot,
    message: Message,
    sender: str,
    *,
    not_mentioned_note: str,
    prepare: PrepareFn,
) -> None:
    if not was_mentioned(message):
        remember_only(bot, message, sender, not_mentioned_note)
        return

    result = await prepare()
    if result is None:
        return  # prepare() уже надіслав повідомлення про помилку сам

    question_text, extra_parts, history_label = result
    await process_and_reply(
        bot, message, sender, question_text,
        extra_parts=extra_parts,
        history_label=history_label,
    )


async def send_idle_message(bot: Bot, chat_id: int) -> None:
    """Формує і надсилає одне проактивне повідомлення в тихий чат."""
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
    """Раз на idle_check_interval_sec проходиться по відомих чатах: якщо
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
        """Фіксує будь-яке повідомлення від людини в чаті + живить
        детектори 'срачу' та 'довгої тиші конкретної людини'."""
        if message.chat.type != "private":
            bot_state.last_human_activity[message.chat.id] = time.time()
            bot_state.idle_message_sent[message.chat.id] = False

            user_id = message.from_user.id if message.from_user else 0
            username = message.from_user.username if message.from_user else ""
            full_name = message.from_user.full_name if message.from_user else ""
            text = message.text or message.caption or ""

            drama_utils.record_message(message.chat.id, user_id, text)
            absence_utils.record_activity(message.chat.id, user_id, username, full_name)

            spawn_background(maybe_intervene_drama(bot, message))
            spawn_background(maybe_poke_absent_user(bot, message))

        return await handler(message, data)

    @dp.message(Command("start", "help"))
    async def cmd_start(message: Message):
        await message.answer(
            "Прівєт, Я Башмак. "
            f"({', '.join(TRIGGER_NAMES)}).\n\n"
            "Для обичних смертних:\n"
            "/help — цей список команд\n"
            "/gadalka — робе прогноз на основі історії чата і контекста\n"
            "Тільки для адміна:\n"
            "/reset — очистити пам'ять поточного чату\n"
            "/status — статус бота (uptime, розмір історії)\n"
            "/model — інфо про модель та ліміти запитів\n"
            "/features — які фіча-флаги зараз увімкнено"
        )

    @dp.message(Command("reset"), IsAdmin())
    async def cmd_reset(message: Message):
        bot_state.history[message.chat.id].clear()
        await message.answer("Пам'ять цього чату очищена 🧹")

    @dp.message(Command("status"), IsAdmin())
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

    @dp.message(Command("model"), IsAdmin())
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

    @dp.message(Command("features"), IsAdmin())
    async def cmd_features(message: Message):
        await message.answer("⚙️ Фіча-флаги\n" + describe_flags())

    @dp.message(Command("gadalka"))
    async def cmd_gadalka(message: Message):
        sender = message.from_user.full_name if message.from_user else "Хтось"
        full_context = get_full_context_raw(message)  # <-- тут, без фільтра
        recent = list(bot_state.history[message.chat.id])[-10:]

        prompt = (
            f"Ти містична гадалка. Зроби коротке (1-2 речення) абсурдно-влучне "
            f"'передбачення дня' для {sender}, обов'язково зачепивши щось "
            f"конкретне з профілю (включно з чутливим/особистим) чи недавньої "
            f"розмови — саме в цьому й прикол.\n"
            f"[Про людину]: {full_context}\n"
            f"[Недавні повідомлення]: {recent}"
        )
        answer = await ask_gemini(
            [{"role": "user", "parts": [{"text": prompt}]}],
            get_current_date_str(),
        )
        await message.reply(f"🔮 {answer}")

    @dp.message(Command("start", "help", "reset", "status", "model", "features"))
    async def cmd_denied(message: Message):
        return  # мовчки ігноруємо чужі спроби викликати адмін-команди

    @dp.message(F.text)
    async def handle_message(message: Message):
        sender = message.from_user.full_name if message.from_user else "Хтось"

        async def prepare() -> PrepareResult:
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

            return question, (extra_parts or None), question

        await dispatch_media_event(
            bot, message, sender,
            not_mentioned_note=message.text,
            prepare=prepare,
        )

    @dp.message(F.photo)
    async def handle_photo(message: Message):
        sender = message.from_user.full_name if message.from_user else "Хтось"
        caption = message.caption or ""

        async def prepare() -> PrepareResult:
            question = strip_trigger(caption, bot_state.BOT_USERNAME) or "Що на цьому фото?"
            image_bytes = await download_or_reply(
                bot, message, message.photo[-1].file_id, "Не вдалось завантажити фото 😔"
            )
            if image_bytes is None:
                return None
            extra_parts = [types.Part.from_bytes(data=image_bytes, mime_type=IMAGE_MIME_JPEG)]
            return question, extra_parts, f"[фото] {question}"

        await dispatch_media_event(
            bot, message, sender,
            not_mentioned_note="[надіслав(-ла) фото]" + (f" {caption}" if caption else ""),
            prepare=prepare,
        )

    @dp.message(F.sticker)
    async def handle_sticker(message: Message):
        sender = message.from_user.full_name if message.from_user else "Хтось"
        sticker = message.sticker
        emoji = sticker.emoji or "🙂"

        # анімовані (.tgs) і відео-стікери (.webm) Gemini vision напряму не
        # їсть — фіксуємо тільки емодзі, без реального аналізу картинки.
        if sticker.is_animated or sticker.is_video:
            async def prepare() -> PrepareResult:
                label = f"[надіслав(-ла) анімований стікер {emoji}]"
                return label, None, label

            await dispatch_media_event(
                bot, message, sender,
                not_mentioned_note=f"[анімований стікер {emoji}]",
                prepare=prepare,
            )
            return

        async def prepare() -> PrepareResult:
            sticker_bytes = await download_or_reply(
                bot, message, sticker.file_id, "Не вдалось завантажити стікер 😔"
            )
            if sticker_bytes is None:
                return None
            extra_parts = [types.Part.from_bytes(data=sticker_bytes, mime_type="image/webp")]
            return (
                f"[надіслав(-ла) стікер, емодзі: {emoji}]",
                extra_parts,
                f"[надіслав(-ла) стікер {emoji}]",
            )

        await dispatch_media_event(
            bot, message, sender,
            not_mentioned_note=f"[надіслав(-ла) стікер {emoji}]",
            prepare=prepare,
        )

    @dp.message(F.animation)
    async def handle_animation(message: Message):
        """GIF в Telegram технічно приходить як mp4 без звуку (F.animation)."""
        sender = message.from_user.full_name if message.from_user else "Хтось"
        caption = message.caption or ""
        animation = message.animation

        async def prepare() -> PrepareResult:
            if animation.file_size and animation.file_size > 20 * 1024 * 1024:
                await message.reply("Гіфка більша за 20 МБ — стільки бот завантажити не може 😔")
                return None

            animation_bytes = await download_or_reply(
                bot, message, animation.file_id, "Не вдалось завантажити гіфку 😔"
            )
            if animation_bytes is None:
                return None

            question = strip_trigger(caption, bot_state.BOT_USERNAME) or "Що відбувається на цій гіфці?"
            extra_parts = [types.Part.from_bytes(data=animation_bytes, mime_type="video/mp4")]
            return question, extra_parts, f"[гіфка] {question}"

        await dispatch_media_event(
            bot, message, sender,
            not_mentioned_note="[надіслав(-ла) гіфку]" + (f" {caption}" if caption else ""),
            prepare=prepare,
        )

    @dp.message(F.video_note)
    async def handle_video_note(message: Message):
        """Кружки: транскрибуємо мовлення так само, як голосові. Транскрипт
        потрібен і для 'просто запамʼятати', і для відповіді — тому
        загальний dispatch_media_event тут не підходить (транскрипція має
        відбутись ДО перевірки was_mentioned)."""
        sender = message.from_user.full_name if message.from_user else "Хтось"
        mentioned = was_mentioned(message)

        # Помилку показуємо лише якщо бота тегнули — інакше мовчки ігноруємо
        # (як і решта "не тегнули" гілок), тому тут звичайний
        # download_telegram_file, а не download_or_reply.
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
            is_voice_input=VOICE_REPLY_ENABLED,
        )

    @dp.message(F.document)
    async def handle_document(message: Message):
        sender = message.from_user.full_name if message.from_user else "Хтось"
        caption = message.caption or ""
        doc = message.document
        file_name = doc.file_name or "файл"

        async def prepare() -> PrepareResult:
            if doc.file_size and doc.file_size > 20 * 1024 * 1024:
                await message.reply("Файл більший за 20 МБ — стільки бот завантажити не може 😔")
                return None

            data = await download_or_reply(bot, message, doc.file_id, "Не вдалось завантажити файл 😔")
            if data is None:
                return None

            text_content, raw_part = extract_document_text(file_name, data, doc.mime_type)

            if text_content is None and raw_part is None:
                await message.reply(
                    f"Не вмію читати такий формат ({file_name}). "
                    "Підтримую PDF, DOCX, XLSX, PPTX і звичайні текстові файли "
                    "(txt, csv, json, md тощо)."
                )
                return None

            question = strip_trigger(caption, bot_state.BOT_USERNAME) or "Опрацюй цей файл і розкажи головне."
            question_text = f"{question}\n\n[Файл: {file_name}]"

            extra_parts = None
            if raw_part is not None:
                extra_parts = [raw_part]
            else:
                question_text += f"\n\nВміст файлу:\n{trim_document_text(text_content)}"

            return question_text, extra_parts, f"[файл {file_name}] {question}"

        await dispatch_media_event(
            bot, message, sender,
            not_mentioned_note=f"[надіслав(-ла) файл {file_name}]" + (f" {caption}" if caption else ""),
            prepare=prepare,
        )
