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


def set_bot_identity(bot_id: int, username: str, full_name: str) -> None:
    global BOT_ID, BOT_USERNAME, BOT_FULL_NAME
    BOT_ID = bot_id
    BOT_USERNAME = username
    BOT_FULL_NAME = full_name
