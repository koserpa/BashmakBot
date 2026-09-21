"""Утиліти для таймерів та нагадувань у чаті (/remind, /reminders, /remind_del).
Працює автономно через asyncio, зберігає список у data/reminders.json.
0 витрат Gemini API.
"""
import asyncio
import datetime
import json
import logging
import os
import re
import time

log = logging.getLogger("Bashma4ek_Bot.reminders")

REMINDERS_FILE = os.path.join(os.path.dirname(__file__), "data", "reminders.json")

_reminders: list[dict] = []
_loaded = False


def _ensure_loaded() -> None:
    global _reminders, _loaded
    if _loaded:
        return
    _loaded = True
    if os.path.exists(REMINDERS_FILE):
        try:
            with open(REMINDERS_FILE, "r", encoding="utf-8") as f:
                _reminders = json.load(f)
        except Exception as e:
            log.error(f"Помилка завантаження нагадувань з {REMINDERS_FILE}: {e}")
            _reminders = []
    else:
        _reminders = []


def _save() -> None:
    try:
        os.makedirs(os.path.dirname(REMINDERS_FILE), exist_ok=True)
        with open(REMINDERS_FILE, "w", encoding="utf-8") as f:
            json.dump(_reminders, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.error(f"Помилка збереження нагадувань у {REMINDERS_FILE}: {e}")


def _next_id() -> int:
    if not _reminders:
        return 1
    return max(r.get("id", 0) for r in _reminders) + 1


def parse_time_spec(text: str) -> tuple[float | None, str]:
    """Парсить часову специфікацію на початку рядка:
    1) Відносний час: '15m', '2h', '45s', '1h30m', '10 хв'
    2) Абсолютний час: '14:30', '8.00' (за поточним локальним часом/Варшава)

    Повертає (target_timestamp, remaining_text) або (None, error_msg).
    """
    text = text.strip()
    if not text:
        return None, "Вкажи час і текст нагадування, наприклад: <code>/remind 15m вимкнути плиту</code>"

    now_ts = time.time()

    # 1. Перевірка на абсолютний час HH:MM або H:MM
    abs_match = re.match(r"^([0-2]?\d)[:.]([0-5]\d)\b\s*(.*)$", text, re.IGNORECASE | re.DOTALL)
    if abs_match:
        hour = int(abs_match.group(1))
        minute = int(abs_match.group(2))
        rest = abs_match.group(3).strip()
        if 0 <= hour <= 23:
            now_dt = datetime.datetime.now()
            target_dt = now_dt.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if target_dt.timestamp() <= now_ts:
                # Якщо цей час сьогодні вже минув — ставимо на завтра
                target_dt += datetime.timedelta(days=1)
            return target_dt.timestamp(), rest

    # 2. Перевірка на відносний час (комбінація годин, хвилин, секунд)
    # Знайдемо всі часові кванти на початку рядка
    pattern = re.compile(
        r"^(\d+)\s*(секунд[иу]?|sec|сек|s|минут[иу]?|хвилин[иу]?|min|хв|мин|m|години?|часов|часа|час|год|hr|h|дней|днів|день|дня|d)(?=[0-9\s.,!?:;_-]|$)\s*",
        re.IGNORECASE,
    )

    total_seconds = 0
    curr_text = text
    found_any = False

    while True:
        m = pattern.match(curr_text)
        if not m:
            break
        found_any = True
        val = int(m.group(1))
        unit = m.group(2).lower()
        curr_text = curr_text[m.end():].strip()

        if unit.startswith(("s", "сек")):
            total_seconds += val
        elif unit.startswith(("m", "хв", "мин")):
            total_seconds += val * 60
        elif unit.startswith(("h", "год", "час")):
            total_seconds += val * 3600
        elif unit.startswith(("d", "ден")):
            total_seconds += val * 86400

    if found_any and total_seconds > 0:
        return now_ts + total_seconds, curr_text

    return None, (
        "Не вдалося розпізнати час нагадування.\n"
        "Приклади:\n"
        "• <code>/remind 15m піти їсти</code>\n"
        "• <code>/remind 1h30m здати проект</code>\n"
        "• <code>/remind 18:00 подивитися розклад</code>"
    )


def add_reminder(chat_id: int, user_id: int, user_mention: str, args_text: str) -> str:
    """Додає нове нагадування."""
    _ensure_loaded()
    target_ts, reminder_text = parse_time_spec(args_text)
    if target_ts is None:
        return reminder_text

    if not reminder_text:
        reminder_text = "нагадування!"

    # Обмеження на максимальний час (наприклад 30 днів) і мінімальний (3 секунди)
    now = time.time()
    diff = target_ts - now
    if diff < 3:
        return "⚠️ Час нагадування має бути хоча б на кілька секунд у майбутньому!"
    if diff > 30 * 86400:
        return "⚠️ Нагадування можна встановлювати максимум на 30 днів вперед."

    r_id = _next_id()
    rem = {
        "id": r_id,
        "chat_id": chat_id,
        "user_id": user_id,
        "user_mention": user_mention,
        "text": reminder_text,
        "created_at": now,
        "remind_at": target_ts,
    }
    _reminders.append(rem)
    _save()

    dt_str = datetime.datetime.fromtimestamp(target_ts).strftime("%H:%M:%S (%d.%m)")
    return (
        f"✅ Нагадування #{r_id} встановлено на <b>{dt_str}</b>:\n"
        f"📝 <i>{reminder_text}</i>"
    )


def list_reminders(chat_id: int) -> str:
    """Показує активні нагадування для поточного чату."""
    _ensure_loaded()
    now = time.time()
    chat_rems = [r for r in _reminders if r.get("chat_id") == chat_id and r.get("remind_at", 0) > now]
    if not chat_rems:
        return "📭 Активних нагадувань у цьому чаті немає."

    lines = ["⏰ <b>Активні нагадування:</b>"]
    for r in sorted(chat_rems, key=lambda x: x.get("remind_at", 0)):
        r_id = r.get("id")
        user = r.get("user_mention", "Хтось")
        text = r.get("text", "")
        remind_at = r.get("remind_at", 0)
        dt_str = datetime.datetime.fromtimestamp(remind_at).strftime("%H:%M:%S (%d.%m)")
        remaining_sec = max(int(remind_at - now), 0)
        m, s = divmod(remaining_sec, 60)
        h, m = divmod(m, 60)
        time_left = f"{h}г {m}хв" if h else (f"{m}хв {s}с" if m else f"{s}с")
        lines.append(f"• #{r_id} [через {time_left} о {dt_str}] ({user}): <i>{text}</i>")

    lines.append("\nЩоб скасувати: <code>/remind_del &lt;номер&gt;</code>")
    return "\n".join(lines)


def delete_reminder(chat_id: int, reminder_id_str: str, user_id: int, is_admin: bool = False) -> str:
    """Видаляє нагадування за ID."""
    _ensure_loaded()
    try:
        r_id = int(reminder_id_str.strip().lstrip("#"))
    except ValueError:
        return "⚠️ Вкажи коректний номер нагадування, наприклад: <code>/remind_del 1</code>"

    target = None
    for r in _reminders:
        if r.get("id") == r_id and r.get("chat_id") == chat_id:
            target = r
            break

    if not target:
        return f"❌ Нагадування #{r_id} не знайдено в цьому чаті."

    # Дозволяємо видаляти автору або адміну
    if target.get("user_id") != user_id and not is_admin:
        return "⛔ Ти можеш скасувати лише власне нагадування."

    _reminders.remove(target)
    _save()
    return f"🗑 Нагадування #{r_id} скасовано."


async def reminders_watcher(bot) -> None:
    """Фоновий воркер: перевіряє настання часу нагадувань і надсилає їх у чат."""
    _ensure_loaded()
    while True:
        try:
            now = time.time()
            due = [r for r in list(_reminders) if r.get("remind_at", 0) <= now]
            for r in due:
                chat_id = r.get("chat_id")
                user = r.get("user_mention", "")
                text = r.get("text", "")
                try:
                    msg = f"⏰ {user}, нагадую:\n<b>{text}</b>"
                    await bot.send_message(chat_id, msg)
                except Exception as e:
                    log.error(f"Не вдалося надіслати нагадування #{r.get('id')} у чат {chat_id}: {e}")
                finally:
                    if r in _reminders:
                        _reminders.remove(r)
            if due:
                _save()
        except Exception as e:
            log.exception(f"Помилка в reminders_watcher: {e}")

        await asyncio.sleep(4)
