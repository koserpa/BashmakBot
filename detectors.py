"""Спільний примітив для 'детекторів' проактивної поведінки
(drama_utils, absence_utils, і будь-які майбутні на кшталт "хтось питає
про гроші" чи "довга мовчанка перед іспитом").

Кожен такий детектор раніше писав власний dict[ключ, timestamp] +
ручну перевірку `time.time() - last < COOLDOWN`. CooldownGate виносить
цей шматок в один клас, тож новий детектор — це просто:

    _cooldown = CooldownGate(MY_COOLDOWN_SEC)
    ...
    if _cooldown.is_ready(chat_id):
        _cooldown.mark(chat_id)   # одразу, до Gemini — щоб не задвоїти
        ...
"""
import time


class CooldownGate:
    """Не дає одній і тій самій дії (за довільним ключем, напр. chat_id
    або (chat_id, user_id)) спрацьовувати частіше, ніж раз на cooldown_sec.

    store можна передати ззовні (напр. існуючий dict з bot_state.py), щоб
    зберегти сумісність з кодом, який звертається до цього стану напряму;
    якщо не передати — заводиться власний dict."""

    def __init__(self, cooldown_sec: float, store: dict | None = None):
        self.cooldown_sec = cooldown_sec
        self._last: dict = store if store is not None else {}

    def is_ready(self, key) -> bool:
        return time.time() - self._last.get(key, 0) >= self.cooldown_sec

    def mark(self, key) -> None:
        self._last[key] = time.time()
