"""Simple plugin example: replies to /hello command."""

from aiogram import types
from aiogram.filters import Command
from aiogram.types import Message

from config import ADMIN_USERNAME


async def cmd_hello(message: Message):
    """Reply to /hello with a friendly greeting."""
    username = message.from_user.username or "Хтось"
    await message.reply(f"Привіт, {username}! 👋 Я BashmakBot з плагінами!")


def register_handlers(dp):
    """Register the /hello command handler."""
    from aiogram.filters import Command as CommandFilter
    dp.message.register(cmd_hello, CommandFilter("hello"))


def register_watchers(bot, background_tasks):
    """No background tasks for this plugin."""
    pass