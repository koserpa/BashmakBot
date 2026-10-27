"""Офлайн-тести vulcan_notify: підписи уроків, diff, формат карток.
Запуск: python test_vulcan_notify.py
"""
from __future__ import annotations

import sys

import vulcan_notify as vn

# Ізоляція: тести НЕ мають марати реальний data/vulcan_notify.json
import os as _os
import tempfile as _tempfile
vn.STATE_FILE = _os.path.join(_tempfile.gettempdir(), "vulcan_notify_tests.json")
if _os.path.exists(vn.STATE_FILE):
    _os.remove(vn.STATE_FILE)


def lesson(date, start, subject, *, canceled=False, notice="", changes=()):
    return {
        "date": date, "start": start, "end": start, "subject": subject,
        "group": "", "teacher": "", "room": "", "canceled": canceled,
        "cancel_notice": notice, "changes": list(changes),
    }


def test_signature() -> None:
    l1 = lesson("2026-10-28", "10:35", "Fizyka")
    l2 = lesson("2026-10-28", "10:35", "Fizyka", canceled=True,
                notice="учнів відпустили")
    assert vn.lesson_signature(l1) != vn.lesson_signature(l2)
    assert vn.lesson_signature(l1) == vn.lesson_signature(
        lesson("2026-10-28", "10:35", "Fizyka"))
    # зміна вчителя теж змінює підпис
    l3 = lesson("2026-10-28", "10:35", "Fizyka", changes=["🔄 Заміна вчителя: X"])
    assert vn.lesson_signature(l1) != vn.lesson_signature(l3)
    print("  підпис уроку ✓")


def test_diff() -> None:
    l_plain = lesson("2026-10-28", "10:35", "Fizyka")
    l_cancel = lesson("2026-10-28", "10:35", "Fizyka", canceled=True,
                      notice="uczniowie zwolnieni do domu")
    # seed: база зберігається тихо
    changed, new_map = vn._compute_changes([l_plain], {}, seed=True)
    assert changed == [] and len(new_map) == 1
    # БЕЗ змін: нічого не злетить
    changed, new_map = vn._compute_changes([l_plain], dict(new_map))
    assert changed == []
    # Відминили урок -> диф
    changed, _ = vn._compute_changes([l_cancel], dict(new_map))
    assert len(changed) == 1 and changed[0]["canceled"]
    # зміну прибрали назад -> ТИХО (не спамимо у зворотний бік)
    changed, _ = vn._compute_changes([l_plain], dict(new_map))
    assert changed == []
    # новий урок ІЗ змінами -> диф
    l_new = lesson("2026-10-29", "08:50", "Chemia",
                   changes=["🔄 Заміна вчителя: Wakat5"])
    changed, _ = vn._compute_changes([l_plain, l_new], dict(new_map))
    assert len(changed) == 1 and changed[0]["subject"] == "Chemia"
    print("  diff змін ✓")


def test_cards() -> None:
    from datetime import date
    d = date(2026, 10, 29)
    card = vn.format_changes_card(_day_name(d), "29.10", [
        lesson("2026-10-29", "08:50", "Chemia", changes=["🔄 Заміна вчителя: Wakat5"]),
    ])
    assert "🔔" in card and "Chemia" in card and "Заміна вчителя" in card
    ev = [
        {"typ": 1, "przedmiotNazwa": "Matematyka", "data": "2026-10-29T00:00:00Z"},
        {"typ": 2, "przedmiotNazwa": "Chemia", "data": "2026-10-29T00:00:00Z"},
    ]
    card2 = vn.format_daybefore_card(d, ev)
    assert "Завтра" in card2 and "Matematyka" in card2 and "Chemia" in card2
    print("  формати карток ✓")


def test_overview() -> None:
    """Багатоденний огляд (/sprawdziany): групування по датах, фільтр минулого."""
    from datetime import date
    today = date(2026, 10, 27)
    ev = [
        {"typ": 1, "przedmiotNazwa": "Matematyka", "data": "2026-10-30T23:00:00.000Z"},
        {"typ": 2, "przedmiotNazwa": "Chemia", "data": "2026-10-30T23:00:00.000Z"},
        {"typ": 1, "przedmiotNazwa": "Niemiecki", "data": "2026-11-04T22:00:00.000Z"},
        {"typ": 1, "przedmiotNazwa": "Stare", "data": "2026-10-20T21:00:00.000Z"},  # минуло
    ]
    txt = vn.format_events_overview(ev, today)
    assert "П'ятниця, 30.10" in txt and "Matematyka" in txt and "Chemia" in txt
    assert "Середа, 04.11" in txt
    assert "Stare" not in txt
    print("  багатоденний огляд ✓")


def test_text_triggers() -> None:
    """Текстові тригери sprawdziany/дз (без слеша)."""
    import plugins.vulcan as pv
    good = [
        "Бот кинь справдзіани",
        "бот шо по дз",
        "справдзіани",
        "кинь kartkówki",
        "/sprawdziany",
        "що по домашці",
        "бот дай домашні",
    ]
    bad = ["", None, "привіт", "розклад на завтра", "дзвінок пролунав"]
    for t in good:
        assert t and pv._TEXT_SPZ_RE.search(t or ""), f"має ловити: {t!r}"
    for t in bad:
        if not t:
            assert not pv._TEXT_SPZ_RE.search(t or ""), f"НЕ має ловити: {t!r}"
    assert not pv._TEXT_SPZ_RE.search("дзвінок пролунав"), "дзвінок ≠ дз"
    print("  текстові тригери sprawdziany ✓")


def test_mute() -> None:
    vn._ensure_loaded()
    vn.toggle_mute(999)
    assert vn.is_muted(999)
    vn.toggle_mute(999)
    assert not vn.is_muted(999)
    print("  mute-тоггл ✓")


def test_prune() -> None:
    """Гігієна state: нотифікації старше 35 днів викидаються, свіжі і
    легаси (True/мусор) лишаються; kartkówka підпісається як typ=3."""
    from datetime import date, timedelta
    today = date(2026, 10, 27)
    ev = {
        "101": (today - timedelta(days=40)).isoformat(),   # старе -> викид
        "102": today.isoformat(),                          # свіже -> лишається
        "103": True,                                       # легаси -> лишається
        "104": "garbage",                                  # не дата -> лишається
    }
    pruned = vn._prune_notified(ev, today)
    assert "101" not in pruned and "102" in pruned
    assert pruned["103"] is True and pruned["104"] == "garbage"
    # Стандартний словарь VULCAN: 1=Sprawdzian, 2=Kartkówka, 3=Praca klasowa,
    # 4=Zadanie domowe. typ=2 → «Kartkówka» підтверджено живим API (Chemia
    # 12.11.2026 в UI — «Kartkówка»), а не «Zadanie domowe», як думали раніше.
    assert vn._EVENT_TYPE_NAMES[1] == "🧪 Sprawdzian"
    assert vn._EVENT_TYPE_NAMES[2] == "📝 Kartkówka"
    assert vn._EVENT_TYPE_NAMES[3] == "📚 Praca klasowa"
    assert vn._EVENT_TYPE_NAMES[4] == "📖 Zadanie domowe"
    card = vn.format_daybefore_card(date(2026, 10, 28), [
        {"typ": 2, "przedmiotNazwa": "Chemia", "data": "2026-10-28T00:00:00Z"},
        {"typ": 4, "przedmiotNazwa": "Polski", "data": "2026-10-28T00:00:00Z"},
    ])
    assert "Kartkówka" in card and "Chemia" in card
    assert "Zadanie domowe" in card and "Polski" in card
    assert "typ 2" not in card                # генерик більше не чіпляється
    print("  prune state + kartkówka ✓")


def _day_name(d):
    import vulcan_utils as vu
    return vu.DAY_NAMES_PL[d.weekday()]


if __name__ == "__main__":
    test_signature()
    test_diff()
    test_cards()
    test_overview()
    test_text_triggers()
    test_mute()
    test_prune()
    print("ALL TESTS OK")
    sys.exit(0)
