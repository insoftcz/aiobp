"""Keep reference to running asyncio tasks"""

import asyncio

from typing import Coroutine


__tasks: set[asyncio.Task] = set()  # to avoid garbage collection by holding reference


def create_task(coroutine: Coroutine, name: str) -> asyncio.Task:
    """Creates task and keeps reference until the task is done

    Argument "name" is optional in asyncio.task(), however we require it
    to make our code easier to debug.
    """
    task = asyncio.create_task(coroutine, name=name)
    __tasks.add(task)
    task.add_done_callback(__tasks.discard)
    return task
