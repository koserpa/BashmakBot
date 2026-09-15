"""Спільний рантайм-стан, який потрібен і хендлерам, і main() —
винесено в окремий модуль без бізнес-логіки, щоб уникнути циклічних
імпортів між bot.py і handlers.py."""
import time
from collections import defaultdict, deque

from config import HISTORY_SIZE

# Заповнюється один раз у main() під час старту — щоб не смикати get_me()
# на кожне повідомлення.
BOT_ID: int | None = None
BOT_USERNAME: str = ""
BOT_FULL_NAME: str = ""

START_TIME = time.time()

# chat_id -> deque of {"role": ..., "parts": [...]}
history: dict[int, deque] = defaultdict(lambda: deque(maxlen=HISTORY_SIZE))

# --- Проактивні повідомлення в тихому чаті ----------------------------------
# chat_id -> час останнього повідомлення від людини (time.time())
last_human_activity: dict[int, float] = {}
# chat_id -> чи вже "вистрелили" проактивним повідомленням за цей період тиші
idle_message_sent: dict[int, bool] = {}

# --- Детектор "срачу" -------------------------------------------------------
# chat_id -> deque of (timestamp, user_id, tension_score) для rate-детекції
message_activity: dict[int, deque] = defaultdict(lambda: deque(maxlen=40))
# chat_id -> час останнього drama-втручання бота (кулдаун)
last_drama_intervention: dict[int, float] = {}

# --- Тиша конкретної людини --------------------------------------------
# (chat_id, user_id) -> час останнього повідомлення ЦІЄЇ людини в цьому чаті
last_user_activity: dict[tuple[int, int], float] = {}
# (chat_id, user_id) -> час останнього підколу про відсутність цієї людини
last_absence_poke: dict[tuple[int, int], float] = {}
# chat_id -> {user_id: (username_lower, full_name)} — хто взагалі писав у чаті
known_chat_users: dict[int, dict] = defaultdict(dict)

def set_bot_identity(bot_id: int, username: str, full_name: str) -> None:
    global BOT_ID, BOT_USERNAME, BOT_FULL_NAME
    BOT_ID = bot_id
    BOT_USERNAME = username
    BOT_FULL_NAME = full_name
