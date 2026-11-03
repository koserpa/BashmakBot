"""Офлайн-тести persona (/swap): перемикання промптів, снапшот, зв'язок
з gemini_client. Запуск: python test_persona.py
"""
from __future__ import annotations

import sys

import persona


def test_toggle_and_prompts() -> None:
    """/swap: два режими, різні промпти, toggle туди-назад."""
    persona.set_mode("default")
    assert persona.mode() == "default"
    default_prompt = persona.current_prompt()
    assert "Башмак" in default_prompt
    assert "Гога" not in default_prompt

    # toggle -> goga
    assert persona.toggle() == "goga"
    goga_prompt = persona.current_prompt()
    assert "Гога" in goga_prompt and "мегобар" in goga_prompt
    assert goga_prompt != default_prompt

    # і назад
    assert persona.toggle() == "default"
    assert persona.current_prompt() == default_prompt
    print("  toggle default<->goga ✓")


def test_set_mode_guards() -> None:
    """Невідомий/той самий режим не змінює стан."""
    persona.set_mode("default")
    assert not persona.set_mode("drunk_polish_dude"), "невідомий режим"
    assert persona.mode() == "default"
    assert not persona.set_mode("default"), "той самий режим — без зміни"
    assert persona.set_mode("goga")
    assert persona.mode() == "goga"
    persona.set_mode("default")
    print("  set_mode: гардси ✓")


def test_snapshot_roundtrip_and_garbage() -> None:
    """Режим переживає снапшот; битий снапшот не ламає стан."""
    persona.set_mode("goga")
    snap = persona._get_snapshot()
    assert snap == {"mode": "goga"}
    persona._apply_snapshot(snap)
    assert persona.mode() == "goga"

    persona._apply_snapshot(None)
    persona._apply_snapshot("garbage")
    persona._apply_snapshot({"mode": "khytryi_rezhym"})
    assert persona.mode() == "goga", "битий/невідомий снапшот не змінює режим"

    persona._apply_snapshot({"mode": "default"})
    assert persona.mode() == "default"
    print("  снапшот + сміття ✓")


def test_gemini_client_uses_persona() -> None:
    """_build_system_instruction читає АКТИВНУ персону на кожен виклик
    (інакше /swap не діяв би без рестарту)."""
    import gemini_client

    persona.set_mode("default")
    sys_default = gemini_client._build_system_instruction("2026-11-03")
    assert "Башмак" in sys_default
    assert "2026-11-03" in sys_default          # технічні додатки на місці
    assert "VOICE:" in sys_default

    persona.set_mode("goga")
    sys_goga = gemini_client._build_system_instruction("2026-11-03")
    assert "Гога" in sys_goga and "Башмак" not in sys_goga
    assert "2026-11-03" in sys_goga             # суфікси технічні — в обох
    assert "VOICE:" in sys_goga

    persona.set_mode("default")
    print("  gemini_client читає активну персону ✓")


if __name__ == "__main__":
    test_toggle_and_prompts()
    test_set_mode_guards()
    test_snapshot_roundtrip_and_garbage()
    test_gemini_client_uses_persona()
    print("ALL TESTS OK")
    sys.exit(0)
