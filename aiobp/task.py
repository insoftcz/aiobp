"""Keep reference to running asyncio tasks"""

import asyncio

from aiobp import log

from typing import Coroutine


__tasks: set[asyncio.Task] = set()  # to avoid garbage collection by holding reference


async def exception_handler(coroutine: Coroutine, name: str) -> Coroutine:
    """Log unhandled exception in asyncio task

    This is needed to get exceptions immediately (otherwise they are displayed on shutdown).
    """
    try:
        await coroutine
    except Exception as error:
        log.critical('Unhandled exception in task "%s"', name, exc_info=error)


def create_task(coroutine: Coroutine, name: str) -> asyncio.Task:
    """Creates task and keeps reference until the task is done

    Argument "name" is optional in asyncio.task(), however we require it
    to make our code easier to debug.
    """
    task = asyncio.create_task(exception_handler(coroutine, name=name), name=name)
    __tasks.add(task)
    task.add_done_callback(__tasks.discard)
    return task
