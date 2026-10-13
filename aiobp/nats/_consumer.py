"""Workers of a durable JetStream pull consumer"""

import asyncio
from contextlib import suppress

from nats.aio.msg import Msg
from nats.errors import ConnectionClosedError
from nats.errors import TimeoutError as NatsTimeoutError
from nats.js import JetStreamContext

from aiobp.logging import log
from aiobp.nats._common import INVALID, EventHandler, decode_payload, invoke_event, wants_headers
from aiobp.task import create_task

# How long a consumer worker waits in a single fetch() before polling again. Idle workers are
# cancelled on shutdown, so this doesn't delay it — it only bounds how stale a fetch can get.
_FETCH_TIMEOUT = 5.0
_FETCH_RETRY_DELAY = 1.0


class Consumer:
    """Workers pulling messages of one durable JetStream consumer"""

    def __init__(
        self,
        psub: JetStreamContext.PullSubscription,
        handler: EventHandler,
        durable: str,
        ack_wait: float,
    ) -> None:
        self.psub = psub
        self.handler = handler
        self.with_headers = wants_headers(handler)
        self.durable = durable
        self.ack_wait = ack_wait
        self.stopping = False
        self.workers: list[asyncio.Task[None]] = []
        self.idle: set[asyncio.Task[None]] = set()  # workers waiting in fetch(), safe to cancel

    def start(self, concurrency: int) -> None:
        self.workers = [create_task(self._work(), f"Consumer[{self.durable}]#{i}") for i in range(concurrency)]

    async def stop(self) -> None:
        """Let workers finish messages they are processing, then unsubscribe"""
        self.stopping = True
        for task in self.idle:
            task.cancel()
        await asyncio.gather(*self.workers, return_exceptions=True)
        with suppress(Exception):
            await self.psub.unsubscribe()

    async def _work(self) -> None:
        task = asyncio.current_task()
        if task is None:  # can't happen, we always run inside a task created by start()
            msg = "Consumer worker must run inside a task"
            raise RuntimeError(msg)
        while not self.stopping:
            self.idle.add(task)
            try:
                msgs = await self.psub.fetch(1, timeout=_FETCH_TIMEOUT)
            except NatsTimeoutError:
                continue
            except ConnectionClosedError:
                return
            except Exception:  # noqa: BLE001 - keep the worker alive, JetStream may recover
                log.trace("Fetching messages for consumer %s failed", self.durable)
                await asyncio.sleep(_FETCH_RETRY_DELAY)
                continue
            finally:
                self.idle.discard(task)

            for message in msgs:
                try:
                    await self._process(message)
                except Exception:  # noqa: BLE001 - e.g. ack failed because connection dropped
                    log.trace("Processing message %s by consumer %s failed", message.subject, self.durable)

    async def _process(self, msg: Msg) -> None:
        data = decode_payload(msg.data)
        if data is INVALID:
            # redelivery can't fix a malformed message, drop it for good
            log.error("Consumer %s got invalid JSON on %s, terminating: %r", self.durable, msg.subject, msg.data[:200])
            await msg.term()
            return

        log.debug("Consumer %s processing %s", self.durable, msg.subject)
        keepalive = create_task(self._keepalive(msg), f"Keepalive[{msg.subject}]")
        # let the keepalive task start, cancelling it before its first step would leave its coroutine never awaited
        await asyncio.sleep(0)
        try:
            await invoke_event(self.handler, self.with_headers, msg, data)
            succeeded = True
        except Exception:  # noqa: BLE001 - NAK so another consumer retries it right away
            log.trace("Consumer %s failed to handle %s", self.durable, msg.subject)
            succeeded = False
        finally:
            keepalive.cancel()

        await (msg.ack() if succeeded else msg.nak())

    async def _keepalive(self, msg: Msg) -> None:
        """Keep a long running message assigned to us instead of letting ack_wait redeliver it"""
        while True:
            await asyncio.sleep(self.ack_wait / 2)
            try:
                await msg.in_progress()
            except Exception as error:  # noqa: BLE001 - connection trouble is reported by Nats already
                log.warning("Can't extend ack_wait of %s for consumer %s: %s", msg.subject, self.durable, error)
                return
