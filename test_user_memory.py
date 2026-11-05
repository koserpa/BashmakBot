"""Офлайн-тести user_memory (авто-факти) + user_profiles (/profile).
Запуск: python test_user_memory.py  (без мережі, без Gemini, без .env-залежностей)
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta

import user_memory as um
import user_profiles as up

# --- ізоляція стану між прогонами ---
um._state = {"last_run": "", "users": {}}
um._names = {}
up._profiles = {}


def test_profiles_override() -> None:
    """/profile: динамічний перекриває статичний, видалення повертає статичний."""
    static_style = up.get_style("koserpa")
    assert static_style, "статичний профіль koserpa має існувати в config"
    assert up.source("koserpa") == "static"
    # username без @ і в іншому регістрі
    assert up.get_style("@KOSERPA") == static_style

    assert up.set_style("koserpa", "Тестовий динамічний профіль")
    assert up.get_style("koserpa") == "Тестовий динамічний профіль"
    assert up.source("koserpa") == "dynamic"
    assert not up.set_style("koserpa", "Тестовий динамічний профіль"), \
        "повторний запис того ж тексту не має вважатись зміною"

    # новий користувач, якого немає в config
    assert up.set_style("new_guy", "Четвертий учасник")
    assert "new_guy" in up.known_usernames()

    # снапшот: roundtrip через getter/setter
    snap = up._get_snapshot()
    up._profiles.clear()
    up._apply_snapshot(snap)
    assert up.get_style("new_guy") == "Четвертий учасник"

    assert up.delete_profile("koserpa")
    assert up.get_style("koserpa") == static_style
    assert up.source("koserpa") == "static"
    assert not up.delete_profile("koserpa"), "друге видалення — нічого не змінює"
    # порожні/сміттєві виклики безпечні
    assert not up.set_style("", "x") and not up.set_style(None, "x")
    assert up.get_style("") == "" and up.get_style(None) == ""
    print("  /profile: перекриття статичного, снапшот, видалення ✓")


def test_parse_facts_json() -> None:
    """Відповідь моделі: чистий JSON, у ```-паркані, сміття."""
    good = '{"koserpa": ["купив клавіатуру", "скоро іспит"]}'
    assert um._parse_facts_json(good) == {"koserpa": ["купив клавіатуру", "скоро іспит"]}
    fenced = 'Ось факти:\n```json\n{"alexx_dream": ["малує 3D"]}\n```'
    assert um._parse_facts_json(fenced) == {"alexx_dream": ["малує 3D"]}
    assert um._parse_facts_json("не json взагалі") == {}
    assert um._parse_facts_json('{"koserpa": "не список"}') == {}
    assert um._parse_facts_json('{"koserpa": ["", "   "]}') == {}
    assert um._parse_facts_json("") == {}
    # довгі факти обрізаються
    long = '{"koserpa": ["' + "х" * 500 + '"]}'
    parsed = um._parse_facts_json(long)
    assert max(len(f) for f in parsed["koserpa"]) <= 200
    print("  parse JSON відповіді моделі ✓")


def test_merge_dedupe_cap_ttl() -> None:
    """Злиття фактів: без дублів, кап найновіших, TTL."""
    assert um._merge_facts("koserpa", ["Факт 1", "факт 1 ", "Факт 2"], "2026-11-03")
    users = um._state["users"]
    assert [f["t"] for f in users["koserpa"]] == ["Факт 1", "Факт 2"]
    # повторний мердж тих самих — без змін
    assert not um._merge_facts("koserpa", ["Факт 1"], "2026-11-03")

    # кап: тримаються НАЙНОВІШІ (в кінці)
    old_max = __import__("config").USER_MEMORY_MAX_FACTS
    __import__("config").USER_MEMORY_MAX_FACTS = 2
    try:
        assert um._merge_facts("koserpa", ["Факт 3"], "2026-11-04")
        assert [f["t"] for f in users["koserpa"]] == ["Факт 2", "Факт 3"]
    finally:
        __import__("config").USER_MEMORY_MAX_FACTS = old_max

    # TTL: факт старше 60 днів викидається
    users["koserpa"].append({"t": "Давнє", "d": "2026-01-01"})
    users["ghost"] = [{"t": "ще давніше", "d": "2025-01-01"}]
    pruned = um._prune(users, datetime(2026, 11, 3))
    assert "ghost" not in pruned
    assert all(f["t"] != "Давнє" for f in pruned["koserpa"])
    print("  merge/cap/TTL фактів ✓")


def test_resolve_and_memory_text() -> None:
    """Мапінг username <-> Telegram-ім'я + формат пам'яті для інжекту."""
    um.note_user("koserpa", "Костя Кравченко")
    assert um._resolve_username("koserpa") == "koserpa"
    assert um._resolve_username("@koserpa") == "koserpa"
    assert um._resolve_username("Костя Кравченко") == "koserpa"
    assert um._resolve_username("невідомий") == ""

    um._merge_facts("koserpa", ["Готується до sprawdzian з chemii"], "2026-11-03")
    text = um.get_memory_text("koserpa")
    assert "Пам'ять про людину" in text
    assert "(03.11)" in text and "sprawdzian з chemii" in text
    # промах за username -> знаходить за Telegram-іменем
    assert um.get_memory_text(None, "Костя Кравченко") == text
    assert um.get_memory_text("totally-unknown-user") == ""
    print("  resolve username/ім'я + інжект-текст ✓")


def test_run_once_with_fake_gemini() -> None:
    """Повний цикл run_once з підміненим Gemini: факти пишуться, день
    фіксується, повторного виклику того ж дня немає."""
    import bot_state
    import gemini_client

    # ізоляція: попередні тести лишили факти
    um._state = {"last_run": "", "users": {}}
    um._names = {"koserpa": "Костя Кравченко"}

    bot_state.clear_history(1)
    bot_state.append_history(1, {"role": "user", "parts": [
        {"text": "Костя Кравченко: купив нову відеокарту, тепер CS піде"}]})
    bot_state.append_history(1, {"role": "model", "parts": [{"text": "норм, вітаю"}]})

    um.note_user("koserpa", "Костя Кравченко")
    calls = []

    async def fake_ask(contents, current_date_str, **kwargs):
        calls.append(current_date_str)
        # службовий виклик мусить іти поза персоною
        assert kwargs.get("use_persona") is False
        return '```json\n{"koserpa": ["купив нову відеокарту"]}\n```'

    saved_ask = gemini_client.ask_gemini
    gemini_client.ask_gemini = fake_ask
    try:
        today = datetime(2026, 11, 3, 21, 30)
        assert asyncio.run(um.run_once(today)) is True
        assert len(calls) == 1
        assert um._state["last_run"] == "2026-11-03"
        assert [f["t"] for f in um._state["users"]["koserpa"]] == \
            ["купив нову відеокарту"]
        # той самий день: Gemini НЕ викликається знову
        assert asyncio.run(um.run_once(today)) is True
        assert len(calls) == 1
        # снапшот roundtrip
        snap = um._get_snapshot()
        um._state = {"last_run": "", "users": {}}
        um._apply_snapshot(snap)
        assert um.facts_count("koserpa") == 1
        # невідомі ключі від моделі не падають і не пишуться
        um._merge_facts("koserpa", ["другий факт"], "2026-11-03")
        assert um.facts_count("koserpa") == 2
    finally:
        gemini_client.ask_gemini = saved_ask
    print("  run_once: цикл із фейковим Gemini + снапшот ✓")


def test_snapshot_survives_garbage() -> None:
    """Битий/чужий снапшот не ламає стан."""
    um._apply_snapshot(None)
    um._apply_snapshot("garbage")
    um._apply_snapshot({"users": {"koserpa": ["рядок замість списку", {"t": "ok", "d": "2026-11-03"}]}})
    assert um.facts_count("koserpa") == 1
    up._apply_snapshot(None)
    up._apply_snapshot({"profiles": "не dict"})
    print("  битий снапшот не шкодить ✓")


def test_prompt_content() -> None:
    """Промпт: ростер із іменами, заборона приватного, формат JSON."""
    um.note_user("koserpa", "Костя Кравченко")
    prompt, roster_n = um._build_prompt(["Костя Кравченко: привіт"])
    assert "@koserpa" in prompt and "Костя Кравченко" in prompt
    assert roster_n >= 1
    assert "ЗАБОРОНЕНО" in prompt and "НЕ вигадуй" in prompt
    assert '"<username>"' in prompt
    print("  промпт витягування ✓")


if __name__ == "__main__":
    test_profiles_override()
    test_parse_facts_json()
    test_merge_dedupe_cap_ttl()
    test_resolve_and_memory_text()
    test_run_once_with_fake_gemini()
    test_snapshot_survives_garbage()
    test_prompt_content()
    print("ALL TESTS OK")
    sys.exit(0)
