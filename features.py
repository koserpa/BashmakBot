"""Єдине місце для фіча-флагів бота (env-змінні, що вмикають/вимикають
поведінку). Раніше кожен флаг читався локальним os.getenv() у тому файлі,
де використовувався — щоб побачити всі активні фічі, треба було грепати
весь проєкт. Тепер це один файл + команда /features для адміна."""
import os


def _bool_env(name: str, default: str) -> bool:
    return os.getenv(name, default).lower() == "true"


VOICE_REPLY_ENABLED = _bool_env("VOICE_REPLY_ENABLED", "true")
REACT_UNPROMPTED_ENABLED = _bool_env("REACT_UNPROMPTED_ENABLED", "true")
DRAMA_DETECTOR_ENABLED = _bool_env("DRAMA_DETECTOR_ENABLED", "true")
ABSENCE_DETECTOR_ENABLED = _bool_env("ABSENCE_DETECTOR_ENABLED", "true")

REACT_UNPROMPTED_CHANCE = float(os.getenv("REACT_UNPROMPTED_CHANCE", "0.35"))
ABSENCE_POKE_CHANCE = float(os.getenv("ABSENCE_POKE_CHANCE", "0.25"))

_ALL_FLAGS = {
    "VOICE_REPLY_ENABLED": VOICE_REPLY_ENABLED,
    "REACT_UNPROMPTED_ENABLED": REACT_UNPROMPTED_ENABLED,
    "DRAMA_DETECTOR_ENABLED": DRAMA_DETECTOR_ENABLED,
    "ABSENCE_DETECTOR_ENABLED": ABSENCE_DETECTOR_ENABLED,
}


def describe_flags() -> str:
    """Людський звіт про стан усіх флагів — використовується /features."""
    lines = [f"{'✅' if v else '❌'} {name}" for name, v in _ALL_FLAGS.items()]
    lines.append(f"🎲 REACT_UNPROMPTED_CHANCE: {REACT_UNPROMPTED_CHANCE}")
    lines.append(f"🎲 ABSENCE_POKE_CHANCE: {ABSENCE_POKE_CHANCE}")
    return "\n".join(lines)
