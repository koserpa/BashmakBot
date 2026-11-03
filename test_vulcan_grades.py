"""Офлайн-тести оцінок VULCAN: парсинг /api/Oceny, diff, картки.

Запуск: python test_vulcan_grades.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import date

import vulcan_notify as vn
import vulcan_utils as vu

# Ізоляція: тести не мають марати реальний data/vulcan_notify.json
vn.STATE_FILE = os.path.join(tempfile.gettempdir(), "vulcan_grades_tests.json")
if os.path.exists(vn.STATE_FILE):
    os.remove(vn.STATE_FILE)


GRADE_DATA = {
    "ocenyPrzedmioty": [
        {
            "przedmiotNazwa": "Matematyka",
            "nauczyciele": ["Lizurej Olga [OL]"],
            "kolumnyOcenyCzastkowe": [
                {
                    "idKolumny": 111,
                    "kategoriaKolumny": "Kartkówka",
                    "nazwaKolumny": "funkcja liniowa",
                    "oceny": [
                        {"wpis": "6-", "dataOceny": "06.10.2026", "waga": 2.0,
                         "nauczyciel": "Lizurej Olga [OL]",
                         "zmienionaOdOstatniegoLogowania": False,
                         "idOcenaPoprawiona": None},
                        {"wpis": "+", "dataOceny": "07.10.2026", "waga": 1.0,
                         "nauczyciel": "Lizurej Olga [OL]",
                         "zmienionaOdOstatniegoLogowania": False,
                         "idOcenaPoprawiona": None},
                    ],
                },
                {
                    "idKolumny": 112,
                    "kategoriaKolumny": "Aktywność",
                    "nazwaKolumny": "",
                    "oceny": [
                        {"wpis": "5", "dataOceny": "26.10.2026", "waga": 1.0,
                         "nauczyciel": "",
                         "idOcenaPoprawiona": None,
                         "zmienionaOdOstatniegoLogowania": False},
                    ],
                },
            ],
        },
        {
            "przedmiotNazwa": "Zachowanie",
            "nauczyciele": [],
            "kolumnyOcenyCzastkowe": [],
        },
    ],
}


def test_pick_period() -> None:
    periods = [
        {"id": 1, "dataOd": "2026-09-01T00:00:00+02:00",
         "dataDo": "2027-02-11T00:00:00+01:00"},
        {"id": 2, "dataOd": "2027-02-12T00:00:00+01:00",
         "dataDo": "2027-08-31T00:00:00+02:00"},
    ]
    assert vu.pick_period(periods, date(2026, 11, 3))["id"] == 1
    assert vu.pick_period(periods, date(2027, 3, 1))["id"] == 2
    # поза межами -> останній
    assert vu.pick_period(periods, date(2020, 1, 1))["id"] == 2
    assert vu.pick_period([], date(2026, 11, 3)) is None
    assert vu.pick_period([{"dataOd": "x"}], date(2026, 11, 3)) is None
    print("  вибір періоду ✓")


def test_parse_grades() -> None:
    entries = vu.parse_grades(GRADE_DATA)
    assert len(entries) == 3, entries
    assert entries[0]["subject"] == "Matematyka"
    assert entries[0]["value"] == "6-"
    assert entries[0]["weight"] == 2.0
    assert entries[0]["category"] == "Kartkówka"
    assert entries[0]["column"] == "funkcja liniowa"
    assert entries[0]["teacher"] == "Lizurej Olga [OL]"
    # друга оцінка в тому ж стовпчику має інший index
    assert entries[1]["index"] == 1
    assert vu.grade_identity(entries[0]) != vu.grade_identity(entries[1])
    # вчитель з предмета, якщо в оцінці порожньо
    assert entries[2]["teacher"] == "Lizurej Olga [OL]"
    # порожні оцінки/предмети не потрапляють
    assert vu.parse_grades({"ocenyPrzedmioty": []}) == []
    try:
        vu.parse_grades({"bad": 1})
    except vu.VulcanBadData:
        pass
    else:
        raise AssertionError("очікували VulcanBadData")
    print("  парсинг /api/Oceny ✓")


def test_signature() -> None:
    a = vu.parse_grades(GRADE_DATA)[0]
    b = dict(a)
    assert vu.grade_signature(a) == vu.grade_signature(b)
    b["value"] = "5"
    assert vu.grade_signature(a) != vu.grade_signature(b)
    b = dict(a, weight=1.0)
    assert vu.grade_signature(a) != vu.grade_signature(b)
    # зміна викладача/дати теж виправлення
    assert vu.grade_signature(a) != vu.grade_signature(dict(a, teacher="Інший [I]"))
    print("  підпис оцінки ✓")


def test_compute_grade_changes() -> None:
    entries = vu.parse_grades(GRADE_DATA)
    # seed: тихо запам'ятовуємо все
    new, edited, removed, stored = vn._compute_grade_changes(entries, {}, seed=True)
    assert new == [] and edited == [] and removed == []
    assert len(stored) == 3

    # без змін
    new, edited, removed, _ = vn._compute_grade_changes(entries, dict(stored))
    assert new == [] and edited == [] and removed == []

    # нова оцінка
    added = dict(entries[0])
    added["column_id"] = 999
    added["index"] = 0
    added["value"] = "4"
    new, edited, removed, _ = vn._compute_grade_changes(entries + [added], dict(stored))
    assert len(new) == 1 and new[0]["value"] == "4"
    assert edited == [] and removed == []

    # виправлення старої оцінки (6- -> 5)
    fixed = [dict(e) for e in entries]
    fixed[0]["value"] = "5"
    new, edited, removed, _ = vn._compute_grade_changes(fixed, dict(stored))
    assert new == [] and len(edited) == 1
    old, cur = edited[0]
    assert old["value"] == "6-" and cur["value"] == "5"

    # видалення
    new, edited, removed, _ = vn._compute_grade_changes(entries[1:], dict(stored))
    assert new == [] and edited == []
    assert len(removed) == 1 and removed[0]["value"] == "6-"
    print("  diff оцінок ✓")


def test_cards() -> None:
    entries = vu.parse_grades(GRADE_DATA)
    card = vn.format_grades_card([entries[0]], [], [])
    assert "Нові оцінки" in card and "Matematyka" in card
    assert "6-" in card and "вага 2" in card
    assert "Lizurej Olga" in card and "Kartkówka" in card

    card = vn.format_grades_card([], [({"value": "6-", "subject": "Matematyka"}, entries[0])], [])
    assert "виправлено" in card and "6-" in card and "→" in card

    card = vn.format_grades_card([], [], [entries[2]])
    assert "видалено" in card and "5" in card and "вага 1" in card

    # HTML-екранування
    evil = dict(entries[0], subject="<b>X</b>", value="<script>")
    card = vn.format_grades_card([evil], [], [])
    assert "<script>" not in card and "&lt;script&gt;" in card

    # порожньо -> без картки
    assert vn.format_grades_card([], [], []) == ""
    print("  картки оцінок ✓")


def test_split_message() -> None:
    text = "📊 <b>Заголовок</b>\n" + "\n".join(f"• рядок {i}" for i in range(300))
    parts = vn.split_telegram_message(text, limit=500)
    assert len(parts) > 1
    assert "\n".join(parts) == text
    assert all(len(p) <= 500 for p in parts)
    print("  розбиття довгого повідомлення ✓")


if __name__ == "__main__":
    test_pick_period()
    test_parse_grades()
    test_signature()
    test_compute_grade_changes()
    test_cards()
    test_split_message()
    print("ALL TESTS OK")
    sys.exit(0)
