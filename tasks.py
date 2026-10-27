"""Супервізор фонових watcher-ів.

Проблема, яку він вирішує: усі фонові циклі (нагадування, idle-watcher,
date-sync, vulcan-notify) тримались через голе asyncio.create_task().
Якщо такий цикл всрухнеться вночі (напр., несподівана помилка мережі або
nested-пакета), бот продовже жити, але фіча ТИХО помре до наступного
рестарту — і ніхто не зрозуміє, чому заміни перестали приходити.

Рішення: spawn_watch(name, factory, *args)
  - таск-ранер _run() викликає factory(*args) ще раз зверх, ловить Exception,
    логує з traceback і перезапускає з наростаючою затримкою
    (10с -> 20с -> 40с -> ... кап 5 хв);
  - якщо цикл падає частіше, ніж _MAX_RESTARTS разів за добу — супервізор
    самий тормозить (фіча явно хвора, марний спам-луп галасує логи);
  - кожен живий watcher реєструється в WATCHERS (name -> Task),
    звідти /status показує хто живий;
  - CancelledError проходить крізь (чесний shutdown).
"""
from __future__ import annotations

import asyncio
import logging
import time

log = logging.getLogger("Bashma4ek_Bot.watchdog")

# Скільки разів поспіль за добу може впасти watcher, поки супервізор
# ще перезапускає. Якщо частіше — фіча зламана фундаментально.
_MAX_RESTARTS = 5
_WINDOW_SEC = 24 * 3600

# Живі watcher-х: name -> asyncio.Task. (Заповнюється spawn_watch,
# зчитується у /status.)
WATCHERS: dict[str, asyncio.Task] = {}


async def _run(name: str, factory, args: tuple) -> None:
    restarts: list[float] = []
    while True:
        restarts = [t for t in restarts if time.time() - t < _WINDOW_SEC]
        if len(restarts) > _MAX_RESTARTS:
            log.error(f"watcher {name}: падінь >{_MAX_RESTARTS} за добу — "
                      f"супервізор зупиняється, фіча вимкнута")
            return
        try:
            await factory(*args)
        except asyncio.CancelledError:
            raise                       # це рестарт бота, не падіння
        except Exception as e:
            restarts.append(time.time())
            delay = min(300, 5 * 2 ** len(restarts))
            log.exception(f"watcher {name} впав ({type(e).__name__}: {e}) — "
                          f"перезапуск за {delay}с "
                          f"(спроба {len(restarts)}/{_MAX_RESTARTS})")
            await asyncio.sleep(delay)
            continue
        # Короутіна повернулась САМА: вічний watcher так робити не має,
        # але якщо це відбулось — не крутимо марний луп restart.
        WATCHERS.pop(name, None)
        return


def spawn_watch(name: str, factory, *args, tasks_set: set | None = None,
                replace: bool = False) -> asyncio.Task | None:
    """Запустити фоновий цикл під супервізором.

    factory — ф-я, що ЩОРАЗУ створює СВІЖУ короутину (напр.,
    lambda: reminders_watcher(bot)); в ній НЕ треба передактувати wait.
    tasks_set — той самий set, куди plugins складали таски (для
    bookkeepінга); done-callback сам видаляє з нього.
    replace=True — вбити стару таску з таким ім'ям (для hot-restart).
    """
    old = WATCHERS.get(name, None)
    if old is not None:
        if not replace:
            log.warning(f"watcher {name}: вже запущений — другим запуск скасовано")
            return old
        old.cancel()
    try:
        task = asyncio.get_running_loop().create_task(_run(name, factory, args))
    except RuntimeError:                # немає рачка — просимо з in main()
        return None
    WATCHERS[name] = task
    if tasks_set is not None:

        def _on_done(t: asyncio.Task, _set: set = tasks_set) -> None:
            _set.discard(t)
            if not t.cancelled() and t.exception() is not None:
                # супервізор НІКОЛИ не має впасти сам; логуємо і цю страховку
                log.error(f"супервізор {name} впав з помилкою",
                          exc_info=t.exception())

        task.add_done_callback(_on_done)
    log.info(f"watcher {name}: запущено під супервізором")
    return task
