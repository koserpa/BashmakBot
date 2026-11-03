"""Персона бота: перемикання між двома системними промптами (/swap).

  default — звичайний Башмак (config.SYSTEM_PROMPT, env Koyeb перекриває);
  goga    — Гога, бухий грузин (config.GOGA_SYSTEM_PROMPT).

Режим зберігається в снапшоті (секція persona) — переживає редеплой.
Секція реєструється на рівні імпорту: state_store.start() відновлює
тільки зареєстровані секції.

ВАЖЛИВО: gemini_client читає промпт через current_prompt() НА КОЖНИЙ
запит — тому перемикання діє миттєво, без рестарту.
"""
from __future__ import annotations

import logging
from typing import Any

import config
import state_store

log = logging.getLogger("Bashma4ek_Bot.persona")

MODES = ("default", "goga")

_mode: str = "default"


def mode() -> str:
    """Поточний режим: 'default' або 'goga'."""
    return _mode


def current_prompt() -> str:
    """Системний промпт активної персони (для gemini_client)."""
    if _mode == "goga":
        return config.GOGA_SYSTEM_PROMPT
    return config.SYSTEM_PROMPT


def set_mode(new_mode: str) -> bool:
    """Встановити режим явно. True = змінилось."""
    global _mode
    if new_mode not in MODES or new_mode == _mode:
        return False
    _mode = new_mode
    state_store.request_save()
    log.info(f"persona: режим -> {_mode}")
    return True


def toggle() -> str:
    """Переключити на іншу персону. Повертає новий режим."""
    set_mode("goga" if _mode == "default" else "default")
    return _mode


# ---------------- state_store (снапшот) ----------------

def _get_snapshot() -> dict[str, Any]:
    return {"mode": _mode}


def _apply_snapshot(data: Any) -> None:
    global _mode
    if not isinstance(data, dict):
        return
    value = data.get("mode")
    if value in MODES:
        _mode = value
    log.info(f"persona: відновлено режим {_mode}")


state_store.register("persona", _get_snapshot, _apply_snapshot)
