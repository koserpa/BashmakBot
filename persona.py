"""Персона бота: перемикання між системними промптами (/swap).

Реєстр персонт — config.PERSONAS ({mode: {prompt, name, hello}}):
  default — звичайний Башмак (env SYSTEM_PROMPT на Koyeb перекриває);
  goga    — Гога, бухий грузин;
  roza    — Роза, квіткарка-спокусниця (на межі, без явщини).

Режим зберігається в снапшоті (секція persona) — переживає редеплой.
Секція реєструється на рівні імпорту: state_store.start() відновлює
тільки зареєстровані секції.

ВАЖЛИВО: gemini_client читає промпт через current_prompt() НА КОЖНИЙ
запит — перемикання діє миттєво, без рестарту.
"""
from __future__ import annotations

import logging
from typing import Any

import config
import state_store

log = logging.getLogger("Bashma4ek_Bot.persona")

MODES: tuple[str, ...] = tuple(config.PERSONAS)

_mode: str = "default"


def _info(mode: str) -> dict[str, Any]:
    """Запис персони з config.PERSONAS (дефолт для битого mode)."""
    return config.PERSONAS.get(mode) or config.PERSONAS["default"]


def mode() -> str:
    """Поточний режим ('default' / 'goga' / 'roza' / ...)."""
    return _mode


def mode_name(m: str | None = None) -> str:
    """Людська назва персони ('Гога 🇬🇪')."""
    return str(_info(m if m is not None else _mode).get("name") or m or "?")


def current_prompt() -> str:
    """Системний промпт активної персони (для gemini_client)."""
    return str(_info(_mode).get("prompt") or "")


def current_hello() -> str:
    """Привітальне повідомлення активної персони (для відповіді на /swap)."""
    return str(_info(_mode).get("hello") or "")


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
    """Наступна персона по колу. Повертає новий режим."""
    idx = MODES.index(_mode) if _mode in MODES else 0
    set_mode(MODES[(idx + 1) % len(MODES)])
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
