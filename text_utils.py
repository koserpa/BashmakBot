"""Чисті текстові функції (str -> str/bool), винесені окремо від
handlers.py навмисно: жодна з них не залежить від aiogram.Message чи
мережевих викликів, тож їх можна юніт-тестити напряму, без мокання бота:

    from text_utils import strip_trigger
    assert strip_trigger("Башмак, привіт", "bashmak_bot") == "привіт"
"""
import re

from config import TRIGGER_NAMES

FORCE_VOICE_TRIGGERS = {
    "голосом", "войсом", "озвуч", "начитай", "проговори",
    "скинь войс", "скажи вголос",
}

_NAMES_ALT = "|".join(re.escape(n) for n in TRIGGER_NAMES)
NAME_PATTERN = re.compile(
    rf"(^\s*({_NAMES_ALT})\b)|(\b({_NAMES_ALT})\s*[,!?.]*\s*$)",
    re.IGNORECASE,
) if TRIGGER_NAMES else None


def wants_forced_voice(text: str) -> bool:
    text_lower = (text or "").lower()
    return any(t in text_lower for t in FORCE_VOICE_TRIGGERS)


def strip_trigger(text: str, bot_username: str) -> str:
    """Прибирає @згадку бота та тригер-ім'я з тексту питання."""
    text = text or ""
    text = text.replace(f"@{bot_username}", "")
    if NAME_PATTERN:
        text = NAME_PATTERN.sub("", text, count=1)
    return text.strip(" ,:.!?-")


def strip_name_prefix(text: str, sender: str, bot_name: str) -> str:
    """Прибирає префікс на кшталт 'koserpa: ' або 'Башмак: ' з відповіді моделі."""
    text = text.strip()
    names = "|".join(re.escape(n) for n in {sender, bot_name, *TRIGGER_NAMES} if n)
    if not names:
        return text
    text = re.sub(rf"^\s*(?:{names})\s*:\s*", "", text, count=1, flags=re.IGNORECASE)
    return text.strip()


def text_mentions_bot_username(text: str, bot_username: str) -> bool:
    if not text or not bot_username:
        return False
    return f"@{bot_username}".lower() in text.lower()


def text_mentions_trigger_name(text: str) -> bool:
    return bool(NAME_PATTERN and text and NAME_PATTERN.search(text))


def get_mentioned_usernames(text: str) -> set[str]:
    """Всі @згадки в тексті (без @), для пошуку профілів згаданих людей."""
    return set(re.findall(r"@(\w+)", text or ""))
