"""Plugin system for BashmakBot.

Each plugin is a Python module in the plugins/ folder that exposes:
- register_handlers(dp) — register aiogram handlers
- register_watchers(bot, tasks) — optional: register background tasks

Plugins are auto-loaded on bot startup from config.ENABLED_PLUGINS.
"""
import importlib
import logging

from config import ENABLED_PLUGINS

log = logging.getLogger("BashmakBot.plugins")


def load_plugins(dp, bot, background_tasks=None):
    """Load all enabled plugins.

    Args:
        dp: aiogram Dispatcher
        bot: aiogram Bot instance
        background_tasks: set of asyncio.Tasks (optional, for watchers)
    """
    loaded = []
    for name in ENABLED_PLUGINS:
        try:
            mod = importlib.import_module(f"plugins.{name}")
            # Register handlers
            if hasattr(mod, "register_handlers"):
                mod.register_handlers(dp)
                log.info(f"Plugin '{name}': handlers registered")
            # Register watchers (background tasks)
            if hasattr(mod, "register_watchers") and background_tasks is not None:
                mod.register_watchers(bot, background_tasks)
                log.info(f"Plugin '{name}': watchers registered")
            loaded.append(name)
        except Exception as e:
            log.error(f"Failed to load plugin '{name}': {e}")
    return loaded