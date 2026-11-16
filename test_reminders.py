"""Офлайн-тести парсера часу нагадувань (/remind) і прав на видалення.
Запуск: python test_reminders.py  (без мережі, data/ не чіпається)
"""
from __future__ import annotations

import datetime
import os
import sys
import tempfile
import time
from zoneinfo import ZoneInfo

import reminder_utils as ru

TZ = ZoneInfo("Europe/Warsaw")


def _patched_storage() -> tuple[str, str]:
    """Тимчасовий файл нагадувань — щоб тести не писали в data/reminders.json."""
    old = ru.REMINDERS_FILE
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    ru.REMINDERS_FILE = path
    ru._reminders = []
    ru._loaded = True
    return old, path


def test_relative_time() -> None:
    """Відносний час: 15m, 1h30m, словом (10 хв), днями."""
    now = time.time()
    ts, rest = ru.parse_time_spec("15m вимкнути плиту")
    assert ts is not None and rest == "вимкнути плиту"
    assert abs(ts - (now + 15 * 60)) < 10

    ts, rest = ru.parse_time_spec("1h30m здати проект")
    assert abs(ts - (now + 90 * 60)) < 10 and rest == "здати проект"

    ts, rest = ru.parse_time_spec("10 хв перерва")
    assert abs(ts - (now + 600)) < 10 and rest == "перерва"

    ts, _ = ru.parse_time_spec("2 дня")
    assert abs(ts - (now + 2 * 86400)) < 10
    print("  відносний час: 15m / 1h30m / 10 хв / 2 дня ✓")


def test_absolute_time() -> None:
    """«18:00 текст»: сьогодні, якщо цей час ще не минув, інакше завтра.
    Незалежно від поточного часу доби target має бути рівно одним із
    цих двох варіантів (тест стабільний і о 3 ночі, і о 23 вечора)."""
    now = datetime.datetime.now(TZ)
    today18 = now.replace(hour=18, minute=0, second=0, microsecond=0)
    variants = {
        today18.timestamp(),
        (today18 + datetime.timedelta(days=1)).timestamp(),
    }

    ts, rest = ru.parse_time_spec("18:00 подивитися розклад")
    assert rest == "подивитися розклад"
    assert ts in variants

    # крапка замість двокрапки теж парситься
    ts, _ = ru.parse_time_spec("8.00 ранкова пара")
    t08 = now.replace(hour=8, minute=0, second=0, microsecond=0)
    assert ts in {t08.timestamp(), (t08 + datetime.timedelta(days=1)).timestamp()}

    # без тексту теж працює (add_reminder підставить «нагадування!»)
    ts, rest = ru.parse_time_spec("18:00")
    assert ts in variants and rest == ""
    print("  абсолютний час: 18:00 / 8.00 (+ перехід на завтра) ✓")


def test_garbage_and_limits() -> None:
    """Сміття дає підказку з прикладами; неіснуюча година не парситься;
    завеликий діапазон парситься, але відсікається в add_reminder."""
    ts, msg = ru.parse_time_spec("колись потім")
    assert ts is None and "18:00" in msg      # у підказці є приклад 18:00

    ts, _ = ru.parse_time_spec("")
    assert ts is None

    ts, _ = ru.parse_time_spec("25:00")       # неіснуюча година — не абсолютний час
    assert ts is None

    ts, _ = ru.parse_time_spec("9999d")
    assert ts is not None                      # парсер розпізнав...
    assert ts - time.time() > 30 * 86400       # ...а add_reminder відфільтрує ліміт
    print("  сміття / невалідний час / ліміт 30 днів ✓")


def test_add_and_delete() -> None:
    """/remind 15m -> запис у майбутнє; видалення: не автор — відмовлено,
    адмін — видаляє. Файл нагадувань підмінено тимчасовим (data/ чисто)."""
    old, path = _patched_storage()
    try:
        resp = ru.add_reminder(42, 111, "@koserpa", "15m вимкнути плиту")
        assert "встановлено" in resp and "#1" in resp
        assert len(ru._reminders) == 1
        rem = ru._reminders[0]
        assert rem["chat_id"] == 42 and rem["remind_at"] > time.time()
        assert rem["text"] == "вимкнути плиту"

        bad = ru.add_reminder(42, 111, "@koserpa", "колись потім")
        assert "Не вдалося" in bad and len(ru._reminders) == 1

        # видалення чужим користувачем — відмовлено
        denied = ru.delete_reminder(42, "1", user_id=999, is_admin=False)
        assert "лише власне" in denied and len(ru._reminders) == 1
        # адмін видаляє чужое; '#' в id теж приймається
        ok = ru.delete_reminder(42, "#1", user_id=999, is_admin=True)
        assert "скасовано" in ok and len(ru._reminders) == 0
    finally:
        ru.REMINDERS_FILE = old
        os.unlink(path)
    print("  add/delete + права на видалення ✓")


if __name__ == "__main__":
    test_relative_time()
    test_absolute_time()
    test_garbage_and_limits()
    test_add_and_delete()
    print("ALL TESTS OK")
    sys.exit(0)
