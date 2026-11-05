"""Профілі користувачів для LLM-контексту.

Два шари:
  статичні  — config.USER_CONTEXT (правляться кодом, переживають усе);
  динамічні — команда /profile (адмін), зберігаються в снапшоті
              (секція user_profiles) і ПЕРЕКРИВАЮТЬ статичні.

Ключі динамічних профілів: "id:<user_id>" для людей, які вже писали в
чат (id стабільний, на відміну від @username, який можна змінити або
не мати); username-ключі — легасі зі старих снапшотів, лишаються
читабельними. Пошук завжди пробує id, потім username, потім статичний.

Поле sensitive вирізано (рішення власника 03.11.2026): особистих фактів
у коді більше немає. Бот знає тільки те, що адмін записав у профіль, і
те, що сам витягнув з чату (user_memory.py).

Секція реєструється на рівні імпорту — state_store.start() відновлює
тільки ті секції, що вже зареєстровані.
"""
from __future__ import annotations

import logging
from typing import Any

import config
import state_store

log = logging.getLogger("Bashma4ek_Bot.user_profiles")

# ключ ("id:<uid>" або username lowercase) -> style
_profiles: dict[str, str] = {}


def _norm(username: str | None) -> str:
    return (username or "").lstrip("@").strip().lower()


def _id_key(user_id: Any) -> str:
    if user_id is None:
        return ""
    if isinstance(user_id, int):
        return f"id:{user_id}"
    s = str(user_id).strip()
    return f"id:{s}" if s.isdigit() else ""


def get_style(user_id: Any = None, username: str | None = None) -> str:
    """Стиль користувача: динамічний id -> динамічний username -> статичний."""
    key_id = _id_key(user_id)
    if key_id and key_id in _profiles:
        return _profiles[key_id]
    uname = _norm(username)
    if uname:
        if uname in _profiles:
            return _profiles[uname]
        # динамічний профіль записаний під id, а знайти треба за username
        # (напр., @згадка в тексті) — резолвимо через реєстр user_memory
        try:
            import user_memory
            rk = user_memory.id_key_for_username(uname)
            if rk and rk in _profiles:
                return _profiles[rk]
        except Exception:
            pass
    ctx = config.USER_CONTEXT.get(uname)
    if isinstance(ctx, dict):
        return str(ctx.get("style") or "")
    if isinstance(ctx, str):                 # backward-compat (рядок замість dict)
        return ctx
    return ""


def known_usernames() -> list[str]:
    """Відомі username: статичні (у порядку config) + динамічні."""
    out = [u.lower() for u in config.USER_CONTEXT]
    for key in _profiles:
        if not key.startswith("id:") and key not in out:
            out.append(key)
    return out


def profile_entries() -> list[tuple[str, str, str]]:
    """Для /profile-списку: [(label, style, source)] по всіх профілях."""
    try:
        import user_memory
    except Exception:
        user_memory = None  # type: ignore[assignment]
    out: list[tuple[str, str, str]] = []
    shown: set[str] = set()
    for key, style in _profiles.items():
        if key.startswith("id:"):
            label = user_memory.display_name(key) if user_memory else key
            src = "dynamic"
        else:
            label = key
            src = "dynamic"
        out.append((label, style, src))
        shown.add(key)
    for u in config.USER_CONTEXT:
        if u.lower() in shown or _id_key_entry_exists_for(u):
            continue
        style = ""
        ctx = config.USER_CONTEXT[u]
        if isinstance(ctx, dict):
            style = str(ctx.get("style") or "")
        elif isinstance(ctx, str):
            style = ctx
        out.append((u, style, "static"))
    return out


def _id_key_entry_exists_for(username: str) -> bool:
    """Чи є динамічний id-профіль для цієї людини (щоб не дублювати
    статичний рядок у списку)."""
    try:
        import user_memory
        rk = user_memory.id_key_for_username(username)
        return bool(rk and rk in _profiles)
    except Exception:
        return False


def source(user_id: Any = None, username: str | None = None) -> str:
    """'dynamic' | 'static' | 'none' — звідки береться профіль."""
    key_id = _id_key(user_id)
    if key_id and key_id in _profiles:
        return "dynamic"
    uname = _norm(username)
    if uname:
        if uname in _profiles:
            return "dynamic"
        try:
            import user_memory
            rk = user_memory.id_key_for_username(uname)
            if rk and rk in _profiles:
                return "dynamic"
        except Exception:
            pass
        if uname in config.USER_CONTEXT:
            return "static"
    return "none"


def set_style(user_id: Any = None, username: str | None = None,
              text: str | None = None) -> bool:
    """Записати динамічний профіль. За наявності user_id ключ — "id:<uid>"
    (стабільний), legacy username-запис тієї ж людини прибирається."""
    key_id = _id_key(user_id)
    uname = _norm(username)
    text = (text or "").strip()
    key = key_id or uname
    if not key or not text:
        return False
    if key_id and uname:
        _profiles.pop(uname, None)     # legacy username-запис тієї ж людини
    if _profiles.get(key) == text:
        return False
    _profiles[key] = text
    state_store.request_save()
    log.info(f"profiles: оновлено профіль {key} ({len(text)} символів)")
    return True


def delete_profile(user_id: Any = None, username: str | None = None) -> bool:
    """Видалити динамічний профіль (id- та/або username-варіант)."""
    key_id = _id_key(user_id)
    uname = _norm(username)
    deleted = False
    for key in (key_id, uname):
        if key and key in _profiles:
            del _profiles[key]
            deleted = True
    if deleted:
        state_store.request_save()
        log.info(f"profiles: видалено динамічний профіль ({key_id or uname})")
    return deleted


def facts_count(user_id: Any = None, username: str | None = None) -> int:
    """Скільки авто-фактів про людину (для /profile)."""
    try:
        import user_memory
        return user_memory.facts_count(user_id, username)
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
