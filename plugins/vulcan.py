"""Plugin: реальний розклад з Dziennik VULCAN (вебверсія, Bytom).

Джерело ДАНИХ ТІЛЬКИ ОДНЕ — портал учня VULCAN. Жодних статичних/в mítих
розкладів: якщо сайт недоступний або сесія протухла — бот чесно каже про
це (форматування даних: vulcan_utils.schedule_text).

Команди (як зі слешем, так і просто словом «розклад ...» у тексті):
  /rozklad [аргумент]     — сьогодні/завтра/післязавтра/день тижня/«через N днів»/дата
  /rozklad тиждень        — реальний тижневий огляд (пн-нд)
  /rozklad_jutro          — розклад на завтра
  /rozklad_status         — діагностика сесії VULCAN (cookie живі? кеш?)

Розпізнаються аргументи:
  (нічого) | сьогодні | сегодня | today -> сьогодні
  завтра | jutro | tomorrow             -> +1 день
  післязавтра | послезавтра | pojutrze  -> +2 дні
  пн/понеділок/вт/вівторок/ср/середа... -> найближчий цей день тижня
  через N днів | za N dni               -> +N днів
  2.11 | 02.11.2026 | 2 листопада | 2 listopada -> конкретна дата
Префікси-прийменники («на ...») відкидаються.
"""
import asyncio
import logging
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import F
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message,
)

import vulcan_utils

log = logging.getLogger("Bashma4ek_Bot.plugins.vulcan")

TZ = ZoneInfo("Europe/Warsaw")


def _vulcan_enabled() -> bool:
    import os
    return os.getenv("VULCAN_ENABLED", "true").strip().lower() != "false"


_TODAY_ALIASES = {"сьогодні", "сегодня", "dzisiaj", "today"}
_TOMORROW_ALIASES = {"завтра", "jutro", "tomorrow"}
_AFTER_TOMORROW_ALIASES = {
    "післязавтра", "послезавтра", "послізавтра", "після завтра",
    "pojutrze", "pozajutrze", "za 2 dni", "day after tomorrow",
}
_WEEK_ALIASES = {"тиждень", "вес тиждень", "week", "tydzień", "календар"}
_RELATIVE_DAYS_RE = re.compile(
    r"^(?:через|za|in)\s+(\d{1,3})\s*(?:дн[аіейивю]+\d*|dni?|days?)$"
)
# Дні тижня + всі aceptні форми
_WEEKDAYS: dict[str, int] = {}
for _num, _names in enumerate([
    ("пн", "понеділок", "понедельник", "poniedziałek", "poniedzialek",
     "понеділка", "poniedziałku"),                                        # 0
    ("вт", "вівторок", "вівторка", "вторник", "вторника", "wtorek",
     "wtorku"),                                                           # 1
    ("ср", "середа", "среда", "середу", "среду", "середи", "środa",
     "sroda", "środę", "srode", "środy"),                                 # 2
    ("чт", "четвер", "четверга", "czwartek", "czwartku"),                 # 3
    ("пт", "п'ятниця", "пятниця", "пятница", "п'ятницю", "пятницу",
     "п'ятниці", "piątek", "piatek", "piątku"),                           # 4
    ("сб", "субота", "суббота", "суботу", "субботу", "суботи",
     "sobota", "sobotę", "sobote"),                                       # 5
    ("нд", "неділя", "неділю", "воскресенье", "воскресіння",
     "niedziela", "niedzielę", "niedziele"),                              # 6
], start=0):
    for _n in _names:
        _WEEKDAYS[_n] = _num

# Слова-«обгортки» перед аргументом, які можна спокійно відкинути:
# «розклад на цю середу» -> «середу»; «на наступну п'ятницю» -> «п'ятницю»
_FILLER_WORDS = (
    "на", "у", "в", "це", "цей", "цю", "ця", "того", "та", "то", "той",
    "наступну", "наступний", "наступне", "наступна", "переднє",
    "мій", "моя", "мого", "свою", "своїй",
)
_FILLER_RE = re.compile(
    r"^(?:" + "|".join(re.escape(w) for w in _FILLER_WORDS if w) + r")\s+",
    re.IGNORECASE,
)


def _strip_fillers(arg: str) -> str:
    """/rozklad на цю середу? -> "середу": відкидає прийменники/займенники,
    ключове слово розклад/расписание/schedule і хвостові знаки. Ітерує,
    бо прийменників бува кілька («на цю середу»)."""
    a = (arg or "").strip().strip(",.?!;:-—").lower()
    # ключове слово, якщо хтось повторно його вписав у тексті
    a = re.sub(r"^(?:розклад|расписан\w*|план\w*|rozklad|schedule)\s*", "", a,
               flags=re.IGNORECASE)
    while True:
        m = _FILLER_RE.match(a)
        if not m:
            break
        a = a[m.end():].strip()
    return a.strip(",.?!;:-—")

# Розпізнавання дат: "2.11", "02.11.2026", "2 листопада", "2 listopada"
_MONTHS: dict[str, int] = {}
for _num, _names in enumerate([
    ("січень", "січня", "январь", "января", "styczeń", "stycznia"),          # 1
    ("лютий", "лютого", "февраль", "февраля", "luty", "lutego"),             # 2
    ("березень", "березня", "март", "марта", "marzec", "marca"),             # 3
    ("квітень", "квітня", "апрель", "апреля", "kwiecień", "kwietnia"),       # 4
    ("травень", "травня", "май", "мая", "may", "maja"),                      # 5
    ("червень", "червня", "июнь", "июня", "czerwiec", "czerwca"),            # 6
    ("липень", "липня", "июль", "июля", "lipiec", "lipca"),                  # 7
    ("серпень", "серпня", "август", "августа", "sierpień", "sierpnia"),      # 8
    ("вересень", "вересня", "сентябрь", "сентября", "wrzesień", "września"), # 9
    ("жовтень", "жовтня", "октябрь", "октября", "październik", "października"),  # 10
    ("листопад", "листопада", "ноябрь", "ноября", "listopad", "listopada"),  # 11
    ("грудень", "грудня", "декабрь", "декабря", "grudzień", "grudnia"),      # 12
], start=1):
    for _n in _names:
        _MONTHS[_n] = _num

_DATE_NUM_RE = re.compile(r"^(\d{1,2})\s*[.\-/]\s*(\d{1,2})(?:\s*[.\-/]\s*(\d{2,4}))?$")
_DATE_WORDS_RE = re.compile(r"^(\d{1,2})\s*(?:-го\s+|го\s+)?([a-zа-яёіїєґńśźćąłó]+)$")


def _strip_prep(arg: str) -> str:
    """Obsolete alias на _strip_fillers (для зворотної сумісності тестів)."""
    return _strip_fillers(arg)


def _resolve_date(day: int, month: int, year: int | None):
    now = datetime.now(TZ).date()
    if year is None:
        y = now.year
        try:
            candidate = date(y, month, day)
        except ValueError:
            return None
        if candidate < now:
            y += 1                                     # дата минула -> наступний рік
            try:
                candidate = date(y, month, day)
            except ValueError:
                return None
        return candidate
    if year < 100:
        year += 2000
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _parse_date_arg(arg: str):
    """"2.11" / "02.11.2026" / "2 листопада" / "2 listopada" -> date | None."""
    arg = _strip_prep(arg).lower()
    if not arg:
        return None
    m = _DATE_NUM_RE.match(arg)
    if m:
        y = int(m[3]) if m[3] else None
        return _resolve_date(int(m[1]), int(m[2]), y)
    m = _DATE_WORDS_RE.match(arg)
    if m:
        day, month = int(m[1]), _MONTHS.get(m[2])
        if month:
            return _resolve_date(day, month, None)
    return None


def _next_weekday(weekday: int) -> date:
    """Найближча дата цього дня тижня (минулий день цього тижня -> наступний тиждень)."""
    now = datetime.now(TZ).date()
    delta = (weekday - now.weekday()) % 7
    return now + timedelta(days=delta)


def _parse_relative_arg(arg: str):
    """"через 3 дні" / "za 3 dni" / "післязавтра" / "вівторок" -> date | None."""
    arg = _strip_prep(arg).lower()
    if not arg:
        return None
    if arg in _AFTER_TOMORROW_ALIASES:
        return datetime.now(TZ).date() + timedelta(days=2)
    m = _RELATIVE_DAYS_RE.match(arg)
    if m:
        return datetime.now(TZ).date() + timedelta(days=int(m[1]))
    idx = _WEEKDAYS.get(arg)
    if idx is not None:
        return _next_weekday(idx)
    return None


def _target_from_arg(arg: str) -> date | None | str:
    """Класифікація аргументу_cmd_rozklad.

    Повертає:
      date         — конкретна цільова дата (сьогодні/завтра/післязавтра/дні/дата);
      "week"       — треба тижневий огляд;
      None         — аргумент не розпізнано взагалі.
    """
    arg = _strip_prep(arg).lower()
    if arg in ("", ) or arg in _TODAY_ALIASES:
        return datetime.now(TZ).date()
    if arg in _TOMORROW_ALIASES:
        return datetime.now(TZ).date() + timedelta(days=1)
    if arg in _AFTER_TOMORROW_ALIASES:
        return datetime.now(TZ).date() + timedelta(days=2)
    if arg in _WEEK_ALIASES:
        return "week"
    d = _parse_relative_arg(arg)
    if d is not None:
        return d
    return _parse_date_arg(arg)


def _schedule_kb() -> InlineKeyboardMarkup:
    """Кнопки під карткою розкладу: сусідні види огляду — в один тап."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⏭ Завтра", callback_data="vulcan:jutro"),
        InlineKeyboardButton(text="📅 Тиждень", callback_data="vulcan:tydzien"),
        InlineKeyboardButton(text="🧪 Sprawdziany", callback_data="vulcan:sprz"),
    ]])


async def _reply_day(message: Message, target: date) -> bool:
    """Відправити розклад на день. False = VULCAN не доступний/не налаштований."""
    if not _vulcan_enabled():
        await message.reply("🚫 Розклад вимкнено (VULCAN_ENABLED=false).")
        return True

    try:
        status, text = await vulcan_utils.schedule_text(target)
        if status == "ok":
            # чат «бачив» розклад -> цінна ціль для проактивних сповіщень
            import vulcan_notify
            vulcan_notify.remember_chat(message.chat.id)
    except vulcan_utils.VulcanDisabled:
        await message.reply(
            "🔐 <b>VULCAN не підключено</b>: змінна <code>VULCAN_COOKIES</code> не задана.\n"
            "Попроси адміна виконати <code>python get_vulcan_cookies.py</code>."
        )
        return True
    except vulcan_utils.VulcanError as e:
        log.warning(f"VULCAN помилка: {e}")
        status, text = "error", (
            "⚠️ <b>VULCAN недоступний</b> — не зміг дістати розклад.\n"
            "Спробуй через хвилину."
        )
    await message.reply(text, reply_markup=_schedule_kb())
    return True


async def cmd_rozklad(message: Message):
    """Головна команда розкладу — реальні дані з Dziennik VULCAN."""
    arg = ""
    if message.text:
        parts = message.text.split(maxsplit=1)
        if len(parts) > 1:
            arg = parts[1].strip()

    target = _target_from_arg(arg)

    if target == "week":
        await cmd_rozklad_tydzien(message)
        return
    if isinstance(target, date):
        await _reply_day(message, target)
        return

    # аргумент зовсім не розпізнано — підказка
    await message.reply(
        "Не зрозумів «" + arg + "». Формати:\n"
        "• /rozklad — сьогодні\n"
        "• /rozklad завтра | післязавтра | вівторок | ср\n"
        "• /rozklad через 3 днів\n"
        "• /rozklad 2.11 або «2 листопада»\n"
        "• /rozklad тиждень"
    )


async def cmd_rozklad_jutro(message: Message):
    """/rozklad_jutro — розклад на завтра."""
    await _reply_day(message, datetime.now(TZ).date() + timedelta(days=1))


async def cmd_rozklad_tydzien(message: Message):
    """Реальний тижневий огляд (VULCAN), пн-нд поточного тижня."""
    if not _vulcan_enabled():
        await message.reply("🚫 Розклад вимкнено (VULCAN_ENABLED=false).")
        return
    try:
        status, text = await vulcan_utils.week_text()
        if status == "ok":
            import vulcan_notify
            vulcan_notify.remember_chat(message.chat.id)
    except vulcan_utils.VulcanDisabled:
        await message.reply(
            "🔐 <b>VULCAN не підключено</b>: змінна <code>VULCAN_COOKIES</code> не задана."
        )
        return
    except vulcan_utils.VulcanError as e:
        log.warning(f"VULCAN (тиждень) помилка: {e}")
        status, text = "error", (
            "⚠️ <b>VULCAN недоступний</b> — не зміг дістати тижневий розклад.\n"
            "Спробуй через хвилину."
        )
    await message.reply(text + "\n\n<i>⬇ кнопки внизу — сусідні огляди в один тап</i>",
                        reply_markup=_schedule_kb())


async def on_vulcan_callback(cq: CallbackQuery) -> None:
    """Кнопки під розкладом: 'vulcan:jutro' / 'vulcan:tydzien' / 'vulcan:sprz'."""
    data = cq.data or ""
    action = data[len("vulcan:"):]
    await cq.answer()
    msg = cq.message
    if msg is None:            # старе повідомлення стало недоступним
        return
    if action == "jutro":
        await _reply_day(msg, datetime.now(TZ).date() + timedelta(days=1))
    elif action == "tydzien":
        await cmd_rozklad_tydzien(msg)
    elif action == "sprz":
        await cmd_sprawdziany(msg)


async def cmd_rozklad_status(message: Message):
    """/rozklad_status — стан інтеграції (без секретів)."""
    vulcan_state = await vulcan_utils.status_text()
    text = f"<b>Стан джерел розкладу</b>\n{vulcan_state}"
    await message.reply(text)


async def cmd_sprawdziany(message: Message):
    """/sprawdziany (і текстові варіанти «шо по дз») — всі події на 30 днів."""
    if not _vulcan_enabled():
        await message.reply("🚫 Розклад вимкнено (VULCAN_ENABLED=false).")
        return
    import vulcan_notify
    try:
        status, text = await vulcan_notify.upcoming_events_text(30)
        if status == "ok":
            vulcan_notify.remember_chat(message.chat.id)
    except vulcan_utils.VulcanDisabled:
        await message.reply(
            "🔐 <b>VULCAN не підключено</b>: змінні <code>VULCAN_COOKIES</code> / "
            "<code>VULCAN_LOGIN</code> не задані."
        )
        return
    except vulcan_utils.VulcanError as e:
        log.warning(f"VULCAN (sprawdziany) помилка: {e}")
        text = "⚠️ <b>VULCAN недоступний</b> — спробуй через хвилину."
    await message.reply(text)


async def cmd_vulcan_silence(message: Message):
    """/vulcan_silence — увімкнути/вимкнути проактивні сповіщення для чату."""
    if not _vulcan_enabled():
        await message.reply("🚫 Розклад вимкнено (VULCAN_ENABLED=false).")
        return
    import vulcan_notify
    muted = vulcan_notify.toggle_mute(message.chat.id)
    if muted:
        await message.reply(
            "🔇 <b>Тишину включено</b> — заміни, sprawdziany й ранкові дайджести "
            "в цей чат більше не приходять.\n"
            "Команда ще раз — повернути все назад."
        )
    else:
        await message.reply(
            "🔊 <b>Сповіщення знову активні</b> для цього чату: заміни, "
            "нагадування за день і ранковий дайджест."
        )


# Текстовий тригер: «розклад ...», «Бот розклад ...», «Башмак розклад...».
# Побудований з TRIGGER_NAMES, тож реагує на тіж самі імена, що й чат.
from config import TRIGGER_NAMES

_TRIGGER_ALT = "|".join(re.escape(n) for n in ("бот", *TRIGGER_NAMES))
_TEXT_TRIGGER_RE = re.compile(
    r"^\s*(?:(?:" + _TRIGGER_ALT + r")[\s,.!:;]+)??(?:/rozklad|/schedule|розклад|расписан\w*|план|schedule)\b",
    re.IGNORECASE,
)

# «Бот кинь справдзіани», «шо по дз», «Спраwdzjanи є?» — без команди теж працює.
_TEXT_SPZ_RE = re.compile(
    r"^\s*(?:(?:" + _TRIGGER_ALT + r")[\s,.!?:;]+)??"
    r"(?:кинь|кидай|покажи|дай|даш|шо по|що по|які|какие|какі|буде|будуть|маємо)?\s*"
    r"(?:/sprawdziany|/sprawdzian|sprawdzian\w*|справдз\w*|картківк\w*|kartk\w*"
    r"|\bдз\b|задан\w+\s+домаш\w*|домаш\w+)\b",
    re.IGNORECASE,
)


async def cmd_rozklad_text(message: Message):
    """Text-варіант: «розклад на п'ятницю» (без слеша) теж працює."""
    await cmd_rozklad(message)


async def cmd_sprawdziany_text(message: Message):
    """Text-варіант: «Бот шо по дз» (без слеша) теж працює."""
    await cmd_sprawdziany(message)


def register_handlers(dp) -> None:
    dp.message.register(cmd_rozklad, Command("rozklad", "schedule"))
    dp.message.register(cmd_rozklad_jutro, Command("rozklad_jutro"))
    dp.message.register(cmd_rozklad_tydzien, Command("rozklad_tydzien", "tydzien"))
    dp.message.register(cmd_rozklad_status, Command("rozklad_status", "rozklad_state", "rozklad_statuss"))
    dp.message.register(cmd_sprawdziany, Command("sprawdziany", "sprawdzian", "dz"))
    dp.message.register(cmd_vulcan_silence, Command("vulcan_silence", "vulcan_mute"))
    dp.message.register(cmd_rozklad_text, F.text.regexp(_TEXT_TRIGGER_RE))
    dp.message.register(cmd_sprawdziany_text, F.text.regexp(_TEXT_SPZ_RE))
    dp.callback_query.register(on_vulcan_callback, F.data.startswith("vulcan:"))


def register_watchers(bot, background_tasks) -> None:
    """Тримає сесію VULCAN живою і слідкує за змінами (якщо увімкнено)."""
    if not _vulcan_enabled() or not vulcan_utils.is_configured():
        return

    import config
    if getattr(config, "VULCAN_NOTIFY_ENABLED", True):
        # notify-цикл САМ тримає сесію живою (кожна ітерація = запит до API),
        # тому окремий keepalive тут більше не стартує
        import vulcan_notify
        vulcan_notify.register_watchers(bot, background_tasks)
        return

    # spowіщення вимкнено -> старий Keepalive, щоб сесія не протухала
    import tasks
    if tasks.spawn_watch("vulcan_keepalive",
                         lambda: vulcan_utils.keepalive_watcher(),
                         tasks_set=background_tasks) is None:
        return
    log.info("Vulcan plugin: keepalive зареєстровано (notify вимкнено)")
