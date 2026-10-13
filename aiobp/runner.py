"""Run asyncio coroutine as endless service and handle graceful shutdown"""

import asyncio
import contextlib
import signal
import sys
import time
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

from aiobp.logging import log
from aiobp.task import create_task

__on_shutdown: list[tuple[Callable[..., Coroutine], list[Any], bool]] = []  # to gracefully close connections


def on_shutdown(
    # instead of ..., there should be generic TypeVar reflected to args
    # however python type system doesn't seem to be capable of that yet
    coroutine: Callable[..., Coroutine],
    args: list[Any] | None = None,
    *,
    after_tasks_cancel: bool = False,
) -> None:
    """Register coroutine to be called on graceful shutdown

    Coroutines are called in LIFO order. By default, coroutines are called
    BEFORE all tasks are canceled and awaited for graceful completion.

    If after_tasks_cancel is set to True then the coroutine is called
    AFTER canceling all tasks and waiting for their possible timeout.
    Useful for ClientSession.close() for example.
    """
    if args is None:
        args = []
    __on_shutdown.append((coroutine, args, after_tasks_cancel))
    log_args = ",".join(repr(arg) for arg in args)
    log_when = "after" if after_tasks_cancel else "before"
    log.debug("Will call %s(%s) on shutdown %s tasks cancelation", coroutine, log_args, log_when)


def log_awaitable(awaitable: asyncio.Task | Coroutine) -> str:
    """Make good task description for log"""
    coroutine = awaitable.get_coro() if isinstance(awaitable, asyncio.Task) else awaitable
    if not asyncio.iscoroutine(coroutine):
        return repr(coroutine)

    code = coroutine.cr_code

    cwd = Path.cwd().as_posix()
    filename = code.co_filename
    if filename.startswith(cwd):
        filename = filename[len(cwd) + 1 :]

    if awaitable == coroutine:  # it was coroutine, not a task
        return f'<{filename}:{code.co_firstlineno} method "{code.co_name}">'

    return f'<{awaitable.get_name()} {filename}:{code.co_firstlineno} metod "{code.co_name}">'


def runner(
    service_name: str,
    service_version: str,
    service: Coroutine,
    *,
    shutdown_timeout: float = 5.0,
    endless: bool = True,
) -> None:
    """Run given service in asyncio.Task and handle SIGTERM/KeyboardInterrupt

    Yes, `service_name` and `service_version` arguments are here just for logging,
    because people forget to add version and then it is hell to debug system
    when one is unsure about running version.

    If endless is False, the runner also shuts down as soon as the service
    coroutine finishes on its own; SIGTERM/Ctrl+C still work either way, we
    didn't remove the kill switch. If endless is True (default), the service
    is expected to run forever and only stops on a signal.

    shutdown_timeout is the total time (in seconds) graceful shutdown gets to
    run every on_shutdown() callback and let remaining tasks finish before it
    gives up and logs a warning instead of waiting forever.

    Whatever `service` raises won't crash this function: it's caught, logged
    as critical, and graceful shutdown proceeds anyway.
    """
    log.info("Starting service %s %s on Python %s", service_name, service_version, sys.version.replace("\n", ""))
    loop_runner = asyncio.Runner()
    # main does:
    # 1. start given service coroutine as task
    # 2. add SIGTERM and SIGINT handlers
    # 3. waits for kill or KeyboardInterrupt
    try:
        loop_runner.run(__main(service, endless=endless))
    except KeyboardInterrupt:  # Ctrl+C before __shutdown() installed its signal handlers
        log.info("Interrupted")
    except Exception:  # noqa: BLE001 - yes, we want to catch it and log it
        log.critical("Unhandled exception in service")
    # graceful_shutdown does:
    # 1. calls one by one all registered coroutines via on_shutdown(...) in LIFO order
    # 2. cancel all tasks in parallel (via gather)
    # 3. await tasks to finish in specified shutdown_timeout
    # 4. calls all registered coroutines via on_shutdown(..., after_tasks_cancel=True) in LIFO order
    log.info("Shutting down service %s %s", service_name, service_version)
    loop_runner.run(__graceful_shutdown(shutdown_timeout))
    # Runner.close() cancels remaining tasks and waits for them without any timeout, a task
    # ignoring cancellation would block the exit forever — leave the loop as it is then.
    if not asyncio.all_tasks(loop_runner.get_loop()):
        loop_runner.close()


async def __shutdown() -> None:
    """Wait for CTRL+C or SIGTERM"""
    shutdown_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, shutdown_event.set)
    loop.add_signal_handler(signal.SIGINT, shutdown_event.set)
    with contextlib.suppress(asyncio.CancelledError):
        await shutdown_event.wait()


async def __main(service: Coroutine, *, endless: bool) -> None:
    """Run main service in task"""
    main_task = create_task(service, "MainTask")

    if endless:  # endless wait for interrupt
        await __shutdown()
    else:  # wait for service coroutine to finish or interrupt
        await asyncio.wait(
            [main_task, create_task(__shutdown(), name="Shutdown")],
            return_when=asyncio.FIRST_COMPLETED,
        )


async def __graceful_shutdown(timeout: float = 5.0) -> None:
    """Gracefully shutdown coroutines and tasks"""
    log.info(f"Graceful shutdown up to {timeout:.3f} s...")
    remains = timeout
    remains = await shutdown_coroutines(timeout=remains, after_tasks_cancel=False)
    remains = await __shutdown_tasks(timeout=remains)
    remains = await shutdown_coroutines(timeout=remains, after_tasks_cancel=True)
    if remains > 0:
        log.info("Shutdown completed in %.3f s", (timeout - remains))
    else:
        log.warning("Shutdown not completed within timeout!")


async def shutdown_coroutines(timeout: float, *, after_tasks_cancel: bool) -> float:
    """Wait for all registered on_shutdown callbacks to complete, with a timeout.

    This is normally triggered automatically when SIGTERM or Ctrl+C is received.
    It may also be called explicitly when the main loop is designed to restart
    on failure and already started services need to be stopped gracefully.
    """
    log.debug(f"Shutting down coroutines {'after' if after_tasks_cancel else 'before'} tasks cancel...")

    for coro, args, when in __on_shutdown[::-1]:
        if when != after_tasks_cancel:
            continue

        __on_shutdown.remove((coro, args, when))
        start = time.time()
        log_args = ",".join(repr(arg) for arg in args)
        try:
            coroutine = coro(*args)
        except Exception:  # noqa: BLE001 - yes, we want to catch it and log it
            log.trace("Exception in %s(%s)", coro.__name__, log_args)
            took = time.time() - start
            timeout -= took
            continue

        if not asyncio.iscoroutine(coroutine):  # we may get just plain synchronous method
            took = time.time() - start
            timeout -= took
            continue

        # not wait_for(): on timeout it waits until the cancelled coroutine finishes, possibly forever
        task = asyncio.ensure_future(coroutine)
        await asyncio.wait({task}, timeout=max(timeout, 0))
        if not task.done():
            task.cancel()
            log.error("Coroutine did not finish in timeout: %s(%s)", log_awaitable(coro), log_args)
        elif task.cancelled():
            log.error("Coroutine shutdown cancelled %s(%s)", log_awaitable(coro), log_args)
        elif task.exception() is not None:
            log.error("Coroutine shutdown failed %s(%s): %r", log_awaitable(coro), log_args, task.exception())
        else:
            log.debug("Coroutine finished: %s(%s) -> %r", log_awaitable(coro), log_args, task.result())
        took = time.time() - start
        timeout -= took

    return timeout


async def __shutdown_tasks(timeout: float) -> float:
    """Gather tasks and await for their finish with timeout"""
    log.debug("Shutting down tasks...")

    start = time.time()

    tasks = asyncio.all_tasks()
    tasks.discard(asyncio.current_task())  # don't touch ourselves
    for task in tasks:
        task.cancel()
    # We must await.sleep(0) here so task.cancel() is actually processed
    # and await in the task's coroutine is interrupted.
    await asyncio.sleep(0)

    for task in tasks.copy():
        if __task_failed(task, "%s await must be in try/except!") or task.done():
            # don't wait for failed task because they never gather
            tasks.remove(task)

    if not tasks:
        log.debug("No tasks to gracefully shutdown")
        took = time.time() - start
        return timeout - took

    log.debug(f"Waiting up to {timeout:.3f} s for tasks to finish...")
    # not wait_for(gather()): on timeout it waits for tasks ignoring cancellation, possibly forever
    _, pending = await asyncio.wait(tasks, timeout=max(timeout, 0))
    if pending:
        log.warning("Nope, they get interrupted")
    else:
        log.debug("All tasks finished within timeout")

    for task in tasks:
        if not task.done():
            # Task didn't finish before timeout. Is it a bug in task code executed after cancel
            # or is the timeout too short to finish all needed work by task?
            log.error("Task did not finish gracefully: %s", log_awaitable(task))
            continue

        __task_failed(task, "%s didn't finish in shutdown timeout")

    took = time.time() - start
    return timeout - took


def __task_failed(task: asyncio.Task, canceled_msg: str) -> bool:
    """Log task result/exception and return if exception happened in its coroutine"""
    if not task.done():
        return False

    try:
        result = task.result()
    except asyncio.CancelledError:
        log.trace(canceled_msg, log_awaitable(task))
    except Exception as error:  # noqa: BLE001 - yes, we want to catch it and log it
        log.trace("%s in task %s: %s", error.__class__.__name__, log_awaitable(task), error)
    else:
        log.debug("Task %s finished with result: %r", log_awaitable(task), result)
        return False

    return True
