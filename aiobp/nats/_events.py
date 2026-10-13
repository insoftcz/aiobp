"""Event subscriptions handled one by one in the order events arrived"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from nats.aio.msg import Msg
from nats.aio.subscription import DEFAULT_SUB_PENDING_MSGS_LIMIT, Subscription

from aiobp.logging import log
from aiobp.nats._common import INVALID, EventHandler, decode_payload, invoke_event
from aiobp.task import create_task

_NOT_DECODED = object()


@dataclass(frozen=True)
class HandlerOptions:
    raw: bool
    with_headers: bool


class EventSubscription:
    """One NATS subscription shared by all handlers of the same subject and queue group

    Handlers are called one by one in the order they subscribed. The payload is decoded
    once, only when some handler isn't raw.
    """

    def __init__(self, subscription: Subscription) -> None:
        self.subscription = subscription
        self.handlers: dict[EventHandler, HandlerOptions] = {}

    async def on_message(self, msg: Msg) -> None:
        decoded: Any = _NOT_DECODED
        for handler, options in list(self.handlers.items()):
            if options.raw:
                data: Any = msg.data
            else:
                if decoded is _NOT_DECODED:
                    decoded = decode_payload(msg.data)
                    if decoded is INVALID:
                        log.warning("Invalid JSON event on %s: %r", msg.subject, msg.data[:200])
                if decoded is INVALID:
                    continue
                data = decoded
            try:
                await invoke_event(handler, options.with_headers, msg, data)
            except Exception:  # noqa: BLE001 - one bad handler must not affect the others
                log.trace('Error handling NATS event "%s"', msg.subject)


def _check_internals(subscription: Subscription) -> None:
    """Fail loudly when nats-py changed internals EventQueue relies on, instead of silently losing events"""
    if not (
        isinstance(getattr(subscription, "_pending_queue", None), asyncio.Queue)
        and isinstance(getattr(subscription, "_pending_size", None), int)
        and isinstance(getattr(subscription, "_id", None), int)
    ):
        msg = "Unsupported nats-py version: ordered event handling relies on Subscription internals that changed"
        raise RuntimeError(msg)  # noqa: TRY004 - wrong library version, not a wrong argument


class EventQueue:
    """Handle events of all subscriptions one by one in the order they arrived

    nats-py runs every subscription in its own task, so events of different subscriptions
    get reordered. Only its read loop sees the arrival order, it pushes each message into
    the subscription's `_pending_queue`. All subscriptions share one such queue, consumed
    by a single task here, started by `start()` in the running loop.
    """

    def __init__(self) -> None:
        self.queue: asyncio.Queue[Msg] = asyncio.Queue(maxsize=DEFAULT_SUB_PENDING_MSGS_LIMIT)
        self.task: asyncio.Task[None] | None = None
        self.handlers: dict[int, tuple[Subscription, Callable[[Msg], Awaitable[None]]]] = {}

    @property
    def started(self) -> bool:
        return self.task is not None

    def start(self) -> None:
        self.task = create_task(self._work(), "NATS events")

    def stop(self) -> None:
        """Stop handling events, call after draining the connection"""
        if self.task is not None:
            self.task.cancel()
            self.task = None
        self.handlers.clear()

    def add(self, subscription: Subscription, on_message: Callable[[Msg], Awaitable[None]]) -> None:
        """Route messages of a subscription created without callback (iterator mode) to the queue"""
        _check_internals(subscription)
        own_queue = subscription._pending_queue  # noqa: SLF001 - see class docstring
        subscription._pending_queue = self.queue  # noqa: SLF001
        while not own_queue.empty():  # delivered before we swapped the queue
            self.queue.put_nowait(own_queue.get_nowait())
        self.handlers[subscription._id] = (subscription, on_message)  # noqa: SLF001

    def remove(self, subscription: Subscription) -> None:
        """Forget subscription, its messages still waiting in the queue are dropped"""
        self.handlers.pop(subscription._id, None)  # noqa: SLF001

    async def _work(self) -> None:
        while True:
            msg = await self.queue.get()
            try:
                entry = self.handlers.get(msg.sid)
                if entry is None:  # unsubscribed meanwhile
                    continue
                subscription, on_message = entry
                subscription._pending_size -= len(msg.data)  # noqa: SLF001 - nats-py slow consumer accounting
                await on_message(msg)
            except Exception:  # noqa: BLE001 - keep handling other events
                log.trace('Error handling NATS event "%s"', msg.subject)
            finally:
                self.queue.task_done()  # Subscription.drain() waits for the queue to be processed
