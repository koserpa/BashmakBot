"""Детектор 'срачу' в чаті: якщо повідомлення йдуть часто, від кількох
людей, і в тексті багато маркерів агресії/конфлікту — бот може встряти
з одним коротким коментарем (розрядити жартом або підколоти всіх).

Перевірка активності — повністю детермінована (без викликів Gemini), щоб
не бити по квоті на кожне повідомлення. До Gemini йде рівно один запит —
уже коли поріг перевищено і вирішено, що варто щось написати."""
import logging
import os
import time

import bot_state

log = logging.getLogger("Bashma4ek_Bot.drama")

DRAMA_DETECTOR_ENABLED = os.getenv("DRAMA_DETECTOR_ENABLED", "true").lower() == "true"

DRAMA_WINDOW_SEC = int(os.getenv("DRAMA_WINDOW_SEC", "90"))          # вікно для підрахунку активності
DRAMA_MIN_MESSAGES = int(os.getenv("DRAMA_MIN_MESSAGES", "5"))        # мінімум повідомлень у вікні
DRAMA_MIN_USERS = int(os.getenv("DRAMA_MIN_USERS", "2"))              # мінімум різних людей у вікні
DRAMA_MIN_SCORE = int(os.getenv("DRAMA_MIN_SCORE", "3"))              # поріг сумарної "напруги"
DRAMA_COOLDOWN_SEC = int(os.getenv("DRAMA_COOLDOWN_SEC", str(15 * 60)))  # не частіше разу на N сек на чат

# Список навмисно "м'який" — це лише тригер для виявлення напруги,
# а не список слів, які бот десь відтворює.
DRAMA_MARKER_WORDS = {
    # укр
    "заткнись", "відʼїбись", "бесить", "дратує", "дура", "дурак", "тупий",
    "тупа", "ідіот", "довбойоб", "пішов ти", "пішла ти", "заєбав", "заєбала",
    "срач", "конфлікт", "не почав", "заколебав",
    # рос
    "заткнись", "бесит", "раздражает", "дура", "дурак", "тупой", "тупая",
    "идиот", "долбоеб", "пошел ты", "пошла ты", "задолбал", "задолбала",
    "срач", "конфликт", "заколебал", "иди нахуй", "далбаеб", "нахуй", "нахуй", "блять",
    "сука", "пидор", "пидорас", "даун",
}


def _tension_score(text: str) -> int:
    """Грубий, але дешевий скор 'напруги' одного повідомлення."""
    if not text:
        return 0
    text_lower = text.lower()
    score = sum(1 for w in DRAMA_MARKER_WORDS if w in text_lower)

    letters = [c for c in text if c.isalpha()]
    if len(letters) > 8 and sum(1 for c in letters if c.isupper()) / len(letters) > 0.6:
        score += 1  # СУЦІЛЬНИЙ КАПС

    if text.count("!") >= 3 or text.count("?") >= 3:
        score += 1

    return score


def record_message(chat_id: int, user_id: int, text: str) -> None:
    """Фіксує повідомлення в буфері активності чату — викликається на
    КОЖНЕ повідомлення в групі, незалежно від того, тегнули бота чи ні."""
    if not DRAMA_DETECTOR_ENABLED:
        return

    now = time.time()
    buf = bot_state.message_activity[chat_id]
    buf.append((now, user_id, _tension_score(text)))

    # Буфер має maxlen у bot_state, але додатково чистимо дуже старі
    # записи, щоб is_drama_happening не тягнув зайве по часу.
    while buf and now - buf[0][0] > DRAMA_WINDOW_SEC * 3:
        buf.popleft()


def is_drama_happening(chat_id: int) -> bool:
    """Чи виглядає останній відрізок активності як 'срач'."""
    if not DRAMA_DETECTOR_ENABLED:
        return False

    now = time.time()
    buf = bot_state.message_activity[chat_id]
    recent = [(ts, uid, score) for ts, uid, score in buf if now - ts <= DRAMA_WINDOW_SEC]

    if len(recent) < DRAMA_MIN_MESSAGES:
        return False

    distinct_users = {uid for _, uid, _ in recent}
    if len(distinct_users) < DRAMA_MIN_USERS:
        return False

    total_score = sum(score for _, _, score in recent)
    if total_score < DRAMA_MIN_SCORE:
        return False

    last_time = bot_state.last_drama_intervention.get(chat_id, 0)
    if now - last_time < DRAMA_COOLDOWN_SEC:
        return False

    return True


def mark_drama_handled(chat_id: int) -> None:
    """Ставимо позначку ОДРАЗУ (до звернення до Gemini), щоб паралельні
    повідомлення, що прилетіли одночасно, не спричинили дублікат."""
    bot_state.last_drama_intervention[chat_id] = time.time()
