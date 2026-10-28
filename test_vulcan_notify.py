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


def lesson(date, start, subject, *, canceled=False, notice="", changes=(),
           teacher="", room="", raw_changes=None, raw_notes=()):
    l = {
        "date": date, "start": start, "end": start, "subject": subject,
        "group": "", "teacher": teacher, "room": room, "canceled": canceled,
        "cancel_notice": notice, "changes": list(changes),
    }
    if raw_changes is not None:
        l["raw_changes"] = [list(z) for z in raw_changes]
        l["raw_notes"] = list(raw_notes)
    return l


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
    # порядок змін від порталу не має значення (інакше спам кожні 15 хв)
    a = lesson("2026-10-28", "10:35", "Fizyka",
               changes=["🔄 Заміна вчителя: X", "🕒 Перенесено на 15:00"])
    b = lesson("2026-10-28", "10:35", "Fizyka",
               changes=["🕒 Перенесено на 15:00", "🔄 Заміна вчителя: X"])
    assert vn.lesson_signature(a) == vn.lesson_signature(b)
    print("  підпис уроку ✓")


def test_signature_ignores_wording() -> None:
    """Правка формулювання картки — не нова зміна (інакше деплой = дубль)."""
    raw = [["prowadzacy=Nowak NOWAK", "zmiana=7"]]
    a = lesson("2026-10-29", "12:25", "Chemia",
               changes=["🔄 Заміна вчителя → Nowak NOWAK"], raw_changes=raw)
    b = lesson("2026-10-29", "12:25", "Chemia",
               changes=["🔄 Заміна вчителя: було → Nowak NOWAK"], raw_changes=raw)
    assert vn.lesson_signature(a) == vn.lesson_signature(b)

    # реальна зміна сирих даних -> інший підпис
    c = lesson("2026-10-29", "12:25", "Chemia",
               changes=["🔄 Заміна вчителя → Inny INNY"],
               raw_changes=[["prowadzacy=Inny INNY", "zmiana=7"]])
    assert vn.lesson_signature(a) != vn.lesson_signature(c)

    # порядок zmiany від порталу не має значення
    d = lesson("2026-10-29", "12:25", "Chemia", changes=a["changes"],
               raw_changes=[["zmiana=8", "sala=101"], ["prowadzacy=X", "zmiana=7"]])
    e = lesson("2026-10-29", "12:25", "Chemia", changes=a["changes"],
               raw_changes=[["prowadzacy=X", "zmiana=7"], ["zmiana=8", "sala=101"]])
    assert vn.lesson_signature(d) == vn.lesson_signature(e)
    print("  підпис ігнорує формулювання ✓")


def test_legacy_signature_migration() -> None:
    """State зі старим (текстовим) підписом не дає дубля картки після деплою."""
    raw = [["zajecia=Biologia", "zmiana=15"]]
    l = lesson("2026-10-29", "12:25", "Chemia",
               changes=["🔀 Заміна на: Biologia (4TIR)"], raw_changes=raw)
    old = vn._legacy_signature(l)
    changed, new_map = vn._compute_changes([l], {vn.lesson_key(l): old})
    assert changed == [], "старий підпис не має вважатись новою зміною"
    assert new_map[vn.lesson_key(l)] == vn.lesson_signature(l)

    # а справжня зміна (інший предмет/текст) — ловиться як завжди
    l2 = lesson("2026-10-29", "12:25", "Chemia",
                changes=["🔀 Заміна на: Historia (4TIR)"],
                raw_changes=[["zajecia=Historia", "zmiana=15"]])
    changed, _ = vn._compute_changes([l2], {vn.lesson_key(l2): old})
    assert changed == [l2]
    print("  міграція старого підпису ✓")


def test_health_info() -> None:
    """/health має віддавати реальний стан VULCAN, а не просто «OK»."""
    info = vn.health_info()
    for k in ("configured", "targets", "last_poll_at", "last_poll_ok",
              "last_poll_error", "poll_interval_min", "tracked_lessons"):
        assert k in info, f"немає поля {k}"
    assert isinstance(info["targets"], int)
    vn._mark_poll(False, error="boom")
    assert vn.LAST_POLL["ok"] is False and vn.LAST_POLL["error"] == "boom"
    vn._mark_poll(True, days=42)
    assert vn.LAST_POLL["ok"] is True and vn.LAST_POLL["days"] == 42
    print("  health_info ✓")


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


def test_seed_does_not_swallow_current_changes() -> None:
    """Перший запуск (Koyeb ephemeral) не має «з'їдати» заміни на сьогодні+."""
    from datetime import date
    today = date(2026, 10, 28)
    l_today = lesson("2026-10-28", "10:35", "Fizyka",
                     changes=["🔄 Заміна вчителя: X"])
    changed, _ = vn._compute_changes([l_today], {}, seed=True, today=today)
    assert len(changed) == 1, changed

    l_future = lesson("2026-10-30", "10:35", "Fizyka",
                      changes=["🕒 Перенесено на 15:00"])
    changed, _ = vn._compute_changes([l_future], {}, seed=True, today=today)
    assert len(changed) == 1, changed

    l_past = lesson("2026-10-20", "10:35", "Fizyka",
                    changes=["🔄 Заміна вчителя: old"])
    changed, _ = vn._compute_changes([l_past], {}, seed=True, today=today)
    assert changed == []
    print("  seed не ковтає поточні зміни ✓")


def test_cards() -> None:
    from datetime import date
    d = date(2026, 10, 29)
    card = vn.format_changes_card(_day_name(d), "29.10", [
        lesson("2026-10-29", "12:25", "Język polski w zakresie rozszerzonym",
               teacher="Wakat WAKAT", room="101",
               changes=["🔀 Заміна на: Chemia (4TIR)",
                        "🔄 Заміна вчителя → Nowak NOWAK"]),
    ])
    assert "🔔" in card and "Зміни в розкладі" in card
    assert "Język polski w zakresie rozszerzonym" in card
    # назва уроку — рівно один раз (не дублюється в рядку зміни)
    assert card.count("Język polski w zakresie rozszerzonym") == 1
    # вчитель і кабінет — у рядку уроку, щоб картка була самодостатньою
    assert "Wakat WAKAT" in card and "каб. 101" in card
    assert "🔀 Заміна на: Chemia (4TIR)" in card
    # дві зміни одного уроку — один рядок, а не два
    assert card.count("•") == 1 and "🔄 Заміна вчителя → Nowak NOWAK" in card
    ev = [
        {"typ": 1, "przedmiotNazwa": "Matematyka", "data": "2026-10-29T00:00:00Z"},
        {"typ": 2, "przedmiotNazwa": "Chemia", "data": "2026-10-29T00:00:00Z"},
    ]
    card2 = vn.format_daybefore_card(d, ev)
    assert "Завтра" in card2 and "Matematyka" in card2 and "Chemia" in card2
    print("  формати карток ✓")


def test_html_escaped() -> None:
    """'&'/'<' з порталу не мають ламати HTML (інакше Telegram відхиляє
    повідомлення і зміна не доходить взагалі)."""
    from datetime import date
    card = vn.format_changes_card("Понеділок", "01.01", [
        lesson("2026-01-01", "08:00", "A & B <i>x</i>", changes=["📌 T & U"]),
    ])
    assert "A &amp; B &lt;i&gt;x&lt;/i&gt;" in card
    assert "T &amp; U" in card
    assert "<i>x</i>" not in card
    ev = [{"typ": 1, "przedmiotNazwa": "Mat & Fiz",
           "data": "2026-01-02T00:00:00Z"}]
    assert "Mat &amp; Fiz" in vn.format_daybefore_card(date(2026, 1, 2), ev)
    print("  html.escape у картках ✓")


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


def test_text_triggers_require_address() -> None:
    """«розклад»/«шо по дз» у звичайній розмові не смикають бота —
    потрібне звернення: @username, ім'я-тригер або reply на бота."""
    import bot_state
    import plugins.vulcan as pv

    class Msg:
        def __init__(self, text, reply=None):
            self.text = text
            self.caption = None
            self.reply_to_message = reply

    class User:
        id = 42

    class Reply:
        from_user = User()

    saved_username, saved_id = bot_state.BOT_USERNAME, bot_state.BOT_ID
    bot_state.BOT_USERNAME, bot_state.BOT_ID = "Bashma4ek_Bot", 42
    try:
        assert not pv._addressed_to_bot(Msg("розклад на завтра"))
        assert not pv._addressed_to_bot(Msg("шо по дз"))
        assert pv._addressed_to_bot(Msg("@Bashma4ek_Bot розклад на завтра"))
        assert pv._addressed_to_bot(Msg("на завтра", reply=Reply()))
        if pv.TRIGGER_NAMES:      # у CI без .env імен немає
            assert pv._addressed_to_bot(Msg("башмак, розклад на завтра"))
            assert pv._addressed_to_bot(Msg("розклад на завтра, башмак"))
    finally:
        bot_state.BOT_USERNAME, bot_state.BOT_ID = saved_username, saved_id
    print("  текстові тригери лише зі зверненням ✓")


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


def test_tomorrow_marks_only_after_delivery() -> None:
    """Нагадування «завтра» фіксується лише після успішної доставки.

    Інакше свіжий деплой (у чаті ще немає цілей) позначає подію надісланою
    і вона губиться назавжди.
    """
    import asyncio
    from datetime import date, timedelta

    vn._ensure_loaded()
    saved = (vn._tomorrow_events_fresh, vn._notify, vn.vu.get_client)
    vn._state["notified_events"] = {}

    async def fake_fetch(client):
        return date.today() + timedelta(days=1), [
            {"id": 4242, "typ": 1, "przedmiotNazwa": "Matematyka"},
        ]

    async def fake_get_client():
        return object()

    def fake_notify(delivered: bool):
        async def _f(bot, text):
            return delivered
        return _f

    vn._tomorrow_events_fresh = fake_fetch
    vn.vu.get_client = fake_get_client
    try:
        vn._notify = fake_notify(False)
        assert asyncio.run(vn._tomorrow_cycle(None)) is False
        assert "4242" not in vn._state["notified_events"]

        vn._notify = fake_notify(True)
        assert asyncio.run(vn._tomorrow_cycle(None)) is True
        assert "4242" in vn._state["notified_events"]
    finally:
        (vn._tomorrow_events_fresh, vn._notify, vn.vu.get_client) = saved
    print("  «завтра» фіксується лише після доставки ✓")


def test_morning_retries_when_schedule_fails() -> None:
    """Помилка API о 6:50 не має «закривати» дайджест на всю добу."""
    import asyncio
    import logging
    import vulcan_utils as vu

    saved = vu.schedule_text
    saved_level = vn.log.level
    vn.log.setLevel(logging.CRITICAL)   # очікуваний warning не смітити в CI

    async def broken(d, *, force=False):
        return "error", "boom"

    vu.schedule_text = broken
    try:
        assert asyncio.run(vn._morning_cycle(None)) is False
    finally:
        vu.schedule_text = saved
        vn.log.setLevel(saved_level)
    print("  ранковий дайджест: невдача -> повтор ✓")


def test_group_chat_is_registered() -> None:
    """Групи мають від'ємний chat_id — раніше вони відкидались (картки йшли
    лише в приват)."""
    vn._ensure_loaded()
    gid = -1001234567890
    vn.remember_chat(gid)
    assert gid in vn._state["chat_ids"]
    vn.remember_chat(0)                     # фейковий — не додаємо
    assert 0 not in vn._state["chat_ids"]
    print("  груповий чат реєструється ✓")


def _day_name(d):
    import vulcan_utils as vu
    return vu.DAY_NAMES_PL[d.weekday()]


if __name__ == "__main__":
    test_signature()
    test_signature_ignores_wording()
    test_legacy_signature_migration()
    test_health_info()
    test_diff()
    test_seed_does_not_swallow_current_changes()
    test_cards()
    test_html_escaped()
    test_overview()
    test_text_triggers()
    test_text_triggers_require_address()
    test_mute()
    test_group_chat_is_registered()
    test_tomorrow_marks_only_after_delivery()
    test_morning_retries_when_schedule_fails()
    test_prune()
    print("ALL TESTS OK")
    sys.exit(0)
