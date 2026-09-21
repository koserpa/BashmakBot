"""Утиліти для перегляду розкладу пар (CosinusYoung 15+ 2TC).
Працює повністю автономно без звернення до Gemini або сторонніх API.
Дані зберігаються у data/timetable.json.
"""
import datetime
import json
import logging
import os
from zoneinfo import ZoneInfo

log = logging.getLogger("Bashma4ek_Bot.timetable")

TZ = ZoneInfo("Europe/Warsaw")

TIMETABLE_FILE = os.path.join(os.path.dirname(__file__), "data", "timetable.json")

DAY_KEYS = ["monday", "tuesday", "wednesday", "thursday", "friday"]

DAY_ALIASES = {
    # понеділок
    "monday": "monday", "mon": "monday", "пн": "monday", "понеділок": "monday", "понедельник": "monday",
    "poniedziałek": "monday", "poniedzialek": "monday", "pon": "monday",
    # вівторок
    "tuesday": "tuesday", "tue": "tuesday", "вт": "tuesday", "вівторок": "tuesday", "вторник": "tuesday",
    "wtorek": "tuesday", "wt": "tuesday",
    # середа
    "wednesday": "wednesday", "wed": "wednesday", "ср": "wednesday", "середа": "wednesday", "среда": "wednesday",
    "środa": "wednesday", "sroda": "wednesday", "sr": "wednesday",
    # четвер
    "thursday": "thursday", "thu": "thursday", "чт": "thursday", "четвер": "thursday", "четверг": "thursday",
    "czwartek": "thursday", "czw": "thursday",
    # п'ятниця
    "friday": "friday", "fri": "friday", "пт": "friday", "п'ятниця": "friday", "пятница": "friday",
    "piątek": "friday", "piatek": "friday", "pt": "friday",
}


def load_timetable() -> dict:
    """Завантажує розклад з data/timetable.json."""
    if not os.path.exists(TIMETABLE_FILE):
        return {}
    try:
        with open(TIMETABLE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log.error(f"Помилка завантаження {TIMETABLE_FILE}: {e}")
        return {}


def format_day_schedule(day_data: dict) -> str:
    """Форматує розклад на один день у читабельний вигляд."""
    name = day_data.get("name", "День")
    lessons = day_data.get("lessons", [])
    if not lessons:
        return f"📅 <b>{name}</b>: Пар немає 🎉"

    lines = [f"📅 <b>{name}</b>:"]
    for l in lessons:
        num = l.get("num", "")
        time_str = l.get("time", "")
        subj = l.get("subject", "")
        room = l.get("room", "")
        room_str = f" [каб. {room}]" if room else ""
        lines.append(f"{num}. <code>{time_str}</code> — <b>{subj}</b>{room_str}")
    return "\n".join(lines)


def get_schedule(arg: str = "") -> str:
    """Повертає розклад на запитаний день (сьогодні, завтра, день тижня або весь тиждень)."""
    data = load_timetable()
    schedule = data.get("schedule", {})
    if not schedule:
        return "⚠️ Розклад поки не налаштовано або файл розкладу порожній."

    arg_clean = arg.strip().lower()
    now = datetime.datetime.now(TZ)
    weekday = now.weekday()  # 0: Monday, 6: Sunday

    # Якщо запит на весь тиждень
    if arg_clean in {"тиждень", "все", "всі", "all", "tydzien", "week"}:
        out = ["📚 <b>Розклад на весь тиждень (2TC Cosinus):</b>\n"]
        for k in DAY_KEYS:
            if k in schedule:
                out.append(format_day_schedule(schedule[k]))
                out.append("")
        return "\n".join(out).strip()

    # Якщо вказано конкретний день тижня
    if arg_clean in DAY_ALIASES:
        target_key = DAY_ALIASES[arg_clean]
        day_data = schedule.get(target_key)
        if day_data:
            return format_day_schedule(day_data)

    # Якщо запит "завтра" / "jutro"
    if arg_clean in {"завтра", "jutro", "tomorrow"}:
        next_day = (weekday + 1) % 7
        if next_day >= 5:  # субота або неділя -> показуємо понеділок
            target_key = "monday"
            note = "🏖 Завтра вихідний! Розклад на наступний <b>понеділок</b>:\n\n"
        else:
            target_key = DAY_KEYS[next_day]
            note = "➡️ Розклад на <b>завтра</b>:\n\n"
        day_data = schedule.get(target_key, {})
        return note + format_day_schedule(day_data)

    # Якщо аргумент порожній або "сьогодні" / "dzisiaj"
    if not arg_clean or arg_clean in {"сьогодні", "сегодня", "dzisiaj", "today"}:
        if weekday >= 5:  # Вихідні
            day_data = schedule.get("monday", {})
            return "🏖 Сьогодні вихідний! Розклад на <b>понеділок</b>:\n\n" + format_day_schedule(day_data)

        # Якщо вечір буднього дня (після 16:00), зручніше показати розклад на завтра
        if now.hour >= 16 and not arg_clean:
            next_day = (weekday + 1) % 7
            if next_day >= 5:
                day_data = schedule.get("monday", {})
                return "🌇 Пари на сьогодні закінчились! Розклад на <b>понеділок</b>:\n\n" + format_day_schedule(day_data)
            else:
                day_data = schedule.get(DAY_KEYS[next_day], {})
                return "🌇 Пари на сьогодні закінчились! Розклад на <b>завтра</b>:\n\n" + format_day_schedule(day_data)

        day_data = schedule.get(DAY_KEYS[weekday], {})
        return "📖 Розклад на <b>сьогодні</b>:\n\n" + format_day_schedule(day_data)

    # Незрозумілий аргумент
    return (
        f"❓ Не розпізнав день: <i>{arg}</i>.\n\n"
        "Спробуй: <code>/rozklad</code>, <code>/rozklad завтра</code>, "
        "<code>/rozklad пн</code>, <code>/rozklad тиждень</code>."
    )


_TIMETABLE_KEYWORDS = {
    "розклад", "расписание", "розкладу", "расписания",
    "пари", "пары", "пара", "пару", "уроків", "уроков",
    "plan lekcji", "plany", "lekcje", "lekcji",
}


def is_timetable_query(text: str) -> bool:
    """Чи запитує людина про розклад пар або уроків у звичайному повідомленні."""
    t = (text or "").lower()
    return any(w in t for w in _TIMETABLE_KEYWORDS)


def get_schedule_context(query: str) -> str:
    """Повертає короткий контекст розкладу для підмішування моделі."""
    data = load_timetable()
    schedule = data.get("schedule", {})
    if not schedule:
        return ""

    q = (query or "").lower()
    # Якщо запитують конкретний день
    for alias, key in DAY_ALIASES.items():
        if alias in q:
            day_data = schedule.get(key)
            if day_data:
                return f"\n\n[Розклад занять на {day_data.get('name')}]:\n{format_day_schedule(day_data)}"

    # За замовчуванням даємо весь актуальний розклад
    lines = ["[Актуальний розклад занять 2TC CosinusYoung15+]:"]
    for k in DAY_KEYS:
        if k in schedule:
            lines.append(format_day_schedule(schedule[k]))
    return "\n\n" + "\n\n".join(lines)
