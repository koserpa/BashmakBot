"""Детектор 'де Х?': якщо конкретна відома людина (є в USER_CONTEXT)
довго не писала в чаті, а решта активна — бот може іноді підколоти її
відсутність. На відміну від загального idle-watcher (тиша ВСЬОГО чату),
тут стежимо за тишею ОКРЕМОЇ людини на фоні активного чату."""
import os
import random
import time

import bot_state
from config import USER_CONTEXT
from detectors import CooldownGate
from features import ABSENCE_DETECTOR_ENABLED, ABSENCE_POKE_CHANCE

# Скільки годин тиші від конкретної людини вважається "довгою відсутністю"
ABSENCE_MIN_SILENCE_HOURS = float(os.getenv("ABSENCE_MIN_SILENCE_HOURS", "48"))

# Кулдаун на підкол про ОДНУ й ту саму людину в ОДНОМУ чаті — щоб не
# нити про це в кожному повідомленні, поки вона нарешті не напише.
ABSENCE_POKE_COOLDOWN_SEC = float(os.getenv("ABSENCE_POKE_COOLDOWN_SEC", str(24 * 3600)))

# store=bot_state.last_absence_poke — щоб зберегти сумісність з рештою коду,
# яка тримає рантайм-стан централізовано в bot_state.py.
_cooldown = CooldownGate(ABSENCE_POKE_COOLDOWN_SEC, store=bot_state.last_absence_poke)


def record_activity(chat_id: int, user_id: int, username: str, full_name: str) -> None:
    """Фіксує, що ця людина щойно писала в цьому чаті — викликається на
    КОЖНЕ повідомлення в групі."""
    if not user_id:
        return
    now = time.time()
    bot_state.last_user_activity[(chat_id, user_id)] = now
    bot_state.known_chat_users[chat_id][user_id] = (
        (username or "").lstrip("@").lower(),
        full_name or "",
    )


def find_absent_candidate(chat_id: int, current_user_id: int):
    """Шукає серед відомих учасників чату когось, хто мовчить довше за
    поріг, і про кого давно не підколювали. Повертає (user_id, full_name,
    username, silence_hours) або None. Якщо кандидатів кілька — береться
    той, хто мовчить найдовше."""
    if not ABSENCE_DETECTOR_ENABLED:
        return None

    now = time.time()
    best = None

    for user_id, (username, full_name) in bot_state.known_chat_users[chat_id].items():
        if user_id == current_user_id:
            continue
        if username not in USER_CONTEXT:
            continue  # підколюємо тільки профільованих людей — так є що сказати

        last_seen = bot_state.last_user_activity.get((chat_id, user_id))
        if last_seen is None:
            continue

        silence_hours = (now - last_seen) / 3600
        if silence_hours < ABSENCE_MIN_SILENCE_HOURS:
            continue

        if not _cooldown.is_ready((chat_id, user_id)):
            continue

        if best is None or silence_hours > best[3]:
            best = (user_id, full_name, username, silence_hours)

    return best


def mark_poked(chat_id: int, user_id: int) -> None:
    _cooldown.mark((chat_id, user_id))


def should_roll_poke() -> bool:
    return random.random() < ABSENCE_POKE_CHANCE
