"""Funny plugin: bot tells jokes when asked or randomly."""
import random
from aiogram import types
JOKES = [
    "Чому програмист не може вийти з дому? Бо там відкриті скобки.",
    "Я вчора бот сказав «включи газ» — тепер він чекає 15 хв.",
    "Що каже бот, коли йде в душ? — «Вода, але я текст.»",
    "Чому цей бот такі розумний? Бо його тримають на prompt-і.",
]

async def cmd_joke(message: types.Message):
    await message.reply(random.choice(JOKES))

def register_handlers(dp):
    from aiogram.filters import Command as C
    dp.message.register(cmd_joke, C("joke"))

def register_watchers(bot, tasks):
    pass
