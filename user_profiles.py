"""Профілі користувачів для LLM-контексту.

Два шари:
  статичні  — config.USER_CONTEXT (правляться кодом, переживають усе);
  динамічні — команда /profile (адмін), зберігаються в снапшоті
              (секція user_profiles) і ПЕРЕКРИВАЮТЬ статичні.

Поле sensitive вирізано (рішення власника 03.11.2026): особистих фактів
у коді більше немає. Бот знає тільки те, що адмін записав у профіль, і
те, що сам витягнув з чату (user_memory.py).

Секція реєструється на рівні імпорту — state_store.start() відновлює
тільки ті секції, що вже зареєстровані (див. latent bug з chat_history).
"""
from __future__ import annotations

import logging
from typing import Any

import config
import state_store

log = logging.getLogger("Bashma4ek_Bot.user_profiles")

# username (lowercase) -> style. Динамічні перекривають статичні.
_profiles: dict[str, str] = {}


def _norm(username: str | None) -> str:
    return (username or "").lstrip("@").strip().lower()


def get_style(username: str | None) -> str:
    """Стиль користувача: спершу динамічний (/profile), далі статичний."""
    key = _norm(username)
    if not key:
        return ""
    if key in _profiles:
        return _profiles[key]
    ctx = config.USER_CONTEXT.get(key)
    if isinstance(ctx, dict):
        return str(ctx.get("style") or "")
    if isinstance(ctx, str):                 # backward-compat (рядок замість dict)
        return ctx
    return ""


def known_usernames() -> list[str]:
    """Відомі username: статичні (у порядку config) + динамічні."""
    out = [u.lower() for u in config.USER_CONTEXT]
    for u in _profiles:
        if u not in out:
            out.append(u)
    return out


def source(username: str | None) -> str:
    """'dynamic' | 'static' | 'none' — звідки береться профіль."""
    key = _norm(username)
    if key in _profiles:
        return "dynamic"
    if key in config.USER_CONTEXT:
        return "static"
    return "none"


def set_style(username: str | None, text: str) -> bool:
    """Записати динамічний профіль (перекриває статичний). True = змінилось."""
    key = _norm(username)
    text = (text or "").strip()
    if not key or not text:
        return False
    if _profiles.get(key) == text:
        return False
    _profiles[key] = text
    state_store.request_save()
    log.info(f"profiles: оновлено профіль @{key} ({len(text)} символів)")
    return True


def delete_profile(username: str | None) -> bool:
    """Видалити динамічний профіль (статичний, якщо є, знову набирає сили)."""
    key = _norm(username)
    if key in _profiles:
        del _profiles[key]
        state_store.request_save()
        log.info(f"profiles: видалено динамічний профіль @{key}")
        return True
    return False


def facts_count(username: str | None) -> int:
    """Скільки авто-фактів про людину (для /profile)."""
    try:
        import user_memory
        return user_memory.facts_count(username)
    except Exception:
        return 0


# ---------------- state_store (снапшот) ----------------

def _get_snapshot() -> dict[str, Any]:
    return {"profiles": dict(_profiles)}


def _apply_snapshot(data: Any) -> None:
    if not isinstance(data, dict):
        return
    profiles = data.get("profiles")
    if not isinstance(profiles, dict):
        return
    for user, style in profiles.items():
        key = _norm(user)
        if key and isinstance(style, str) and style.strip():
            _profiles[key] = style.strip()
    log.info(f"profiles: відновлено {len(_profiles)} динамічних профілів")


state_store.register("user_profiles", _get_snapshot, _apply_snapshot)
