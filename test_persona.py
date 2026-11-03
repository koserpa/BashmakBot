"""Офлайн-тести persona (/swap): N персон, перемикання промптів, снапшот,
зв'язок з gemini_client. Запуск: python test_persona.py
"""
from __future__ import annotations

import sys

import config
import persona


def test_personas_registry() -> None:
    """Реєстр config.PERSONAS: у кожної є prompt/name/hello; mode-ключі
    збігаються з persona.MODES."""
    assert set(persona.MODES) == set(config.PERSONAS)
    assert len(persona.MODES) >= 3, "має бути мінімум default/goga/roza"
    for m in persona.MODES:
        entry = config.PERSONAS[m]
        assert entry.get("prompt"), f"{m}: немає промпта"
        assert entry.get("name"), f"{m}: немає назви"
        assert entry.get("hello"), f"{m}: немає привітання"
    # унікальні промпти (не скопійовані)
    prompts = {config.PERSONAS[m]["prompt"] for m in persona.MODES}
    assert len(prompts) == len(persona.MODES)
    print("  реєстр персон ✓")


def test_toggle_cycle() -> None:
    """/swap без аргументу: по колу default -> goga -> roza -> default."""
    persona.set_mode("default")
    assert persona.toggle() == "goga"
    assert persona.toggle() == "roza"
    assert persona.toggle() == "default"
    # прямий вибір
    assert persona.set_mode("roza")
    assert persona.mode() == "roza"
    persona.set_mode("default")
    print("  цикл toggle + прямий вибір ✓")


def test_prompts_distinct() -> None:
    """Кожна персона дає свій промпт з характерними маркерами."""
    persona.set_mode("default")
    default_prompt = persona.current_prompt()
    assert "Башмак" in default_prompt and "Гога" not in default_prompt

    persona.set_mode("goga")
    goga_prompt = persona.current_prompt()
    assert "Гога" in goga_prompt and "мегобар" in goga_prompt

    persona.set_mode("roza")
    roza_prompt = persona.current_prompt()
    assert "Роза" in roza_prompt and "квіткарка" in roza_prompt
    # межі вшиті в промпт: без явних сцен навіть коли просять
    assert "НЕ описуєш" in roza_prompt and "по запису" in roza_prompt
    print("  промпти персон ✓")


def test_names_and_hello() -> None:
    """Назви і привітання беруться з реєстру, а не захардкоджені."""
    persona.set_mode("goga")
    assert persona.mode_name() == config.PERSONAS["goga"]["name"]
    assert "Гога" in persona.current_hello()
    persona.set_mode("roza")
    assert "Роза" in persona.mode_name() and "Роза" in persona.current_hello()
    persona.set_mode("default")
    assert persona.mode_name() == "Башмак"
    print("  назви/привітання ✓")


def test_set_mode_guards() -> None:
    """Невідомий/той самий режим не змінює стан."""
    persona.set_mode("default")
    assert not persona.set_mode("drunk_polish_dude"), "невідомий режим"
    assert persona.mode() == "default"
    assert not persona.set_mode("default"), "той самий режим — без зміни"
    assert persona.set_mode("goga")
    persona.set_mode("default")
    print("  set_mode: гардси ✓")


def test_snapshot_roundtrip_and_garbage() -> None:
    """Режим переживає снапшот; битий снапшот не ламає стан. Старий
    снапшот прод-формату ({"mode": ...}) сумісний."""
    persona.set_mode("roza")
    snap = persona._get_snapshot()
    assert snap == {"mode": "roza"}
    persona._apply_snapshot(snap)
    assert persona.mode() == "roza"

    persona._apply_snapshot(None)
    persona._apply_snapshot("garbage")
    persona._apply_snapshot({"mode": "khytryi_rezhym"})
    assert persona.mode() == "roza", "битий/невідомий снапшот не змінює режим"

    persona._apply_snapshot({"mode": "default"})   # формат як у проді
    assert persona.mode() == "default"
    print("  снапшот + сміття + сумісність ✓")


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

    persona.set_mode("roza")
    sys_roza = gemini_client._build_system_instruction("2026-11-03")
    assert "Роза" in sys_roza and "Башмак" not in sys_roza
    assert "VOICE:" in sys_roza

    persona.set_mode("default")
    print("  gemini_client читає активну персону ✓")


if __name__ == "__main__":
    test_personas_registry()
    test_toggle_cycle()
    test_prompts_distinct()
    test_names_and_hello()
    test_set_mode_guards()
    test_snapshot_roundtrip_and_garbage()
    test_gemini_client_uses_persona()
    print("ALL TESTS OK")
    sys.exit(0)
