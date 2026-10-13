"""NATS message bus connector: events, RPC, JetStream consumers and key-value buckets"""

import asyncio
import re
import socket
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, overload

from nats.aio.client import Client
from nats.aio.msg import Msg
from nats.js import JetStreamContext
from nats.js import errors as js_errors
from nats.js.api import ConsumerConfig

from aiobp.logging import log
from aiobp.nats._bucket import Bucket, K, V
from aiobp.nats._common import EventHandler, encoder, invoke, wants_headers
from aiobp.nats._consumer import Consumer
from aiobp.nats._events import EventQueue, EventSubscription, HandlerOptions
from aiobp.nats._rpc import (
    RPC_OK,
    RPC_PREFIX,
    RpcError,
    RpcHandler,
    RpcProxy,
    log_call,
    parse_reply,
    parse_request,
    short_repr,
)
from aiobp.runner import on_shutdown
from aiobp.task import create_task

if TYPE_CHECKING:
    from nats.aio.subscription import Subscription

# JetStream consumer names may not contain whitespace, ".", "*", ">" or path separators.
_INVALID_DURABLE_CHARS = re.compile(r"[\s.*>/\\]")


@dataclass
class NatsConfig:
    """NATS server connection configuration

    `url` may list several cluster servers separated by commas.
    """

    url: str = "nats://127.0.0.1:4222"


class Nats:
    """NATS message bus connector

    Payloads are JSON (msgspec, so `msgspec.Struct` works too). The connection
    reconnects forever, never receives its own published messages (`no_echo`)
    and is drained on graceful shutdown.

    - events: `publish()` / `subscribe()`
    - RPC: `call()` or `rpc.<method>(...)` to call, `serve()` to handle calls;
      wire format is request `[args, kwargs]` and reply `[1, result]` on success
      or `[code, message]` on failure, sent to subject `rpc.<method>`
    - JetStream: `consume()` for durable work queues, `bucket()` for key-value storage

    `client` and `jetstream` expose the underlying nats-py objects for anything else.
    """

    def __init__(
        self,
        service_name: str,
        config: NatsConfig,
        *,
        location: str | None = None,
        rpc_timeout: float = 2.0,
    ) -> None:
        self._service_name: str = service_name
        self._connection_name: str = f"{service_name}@{location or socket.getfqdn()}"
        self._servers: list[str] = [url.strip() for url in config.url.split(",")]
        self._rpc_timeout: float = rpc_timeout
        self._client: Client = Client()
        self._js: JetStreamContext = self._client.jetstream()
        self._subscriptions: dict[tuple[str, str], EventSubscription] = {}  # by (subject, queue)
        self._subscribe_lock = asyncio.Lock()  # serializes subscribe()/unsubscribe(), each of them awaits the server
        self._events: EventQueue = EventQueue()
        self._rpc_subscriptions: dict[str, Subscription] = {}
        self._rpc_tasks: set[asyncio.Task[None]] = set()
        self._consumers: dict[str, Consumer] = {}
        self._buckets: dict[str, Bucket[Any, Any]] = {}
        self._closing: bool = False

    @property
    def client(self) -> Client:
        """Underlying nats-py client"""
        return self._client

    @property
    def jetstream(self) -> JetStreamContext:
        """Underlying nats-py JetStream context"""
        return self._js

    @property
    def connected(self) -> bool:
        return self._client.is_connected

    @property
    def rpc(self) -> RpcProxy:
        """Call RPC methods as attributes: `await nats.rpc.ami.originate(ext, number=n)`"""
        return RpcProxy(self, ())

    async def connect(self) -> None:
        """Connect to NATS server and disconnect gracefully on shutdown

        May be called again after `disconnect()`.
        """
        if self._events.started:
            msg = "Already connected to NATS"
            raise RuntimeError(msg)
        log.info("Connecting to NATS as %s at %s", self._connection_name, ", ".join(self._servers))
        await self._client.connect(
            servers=self._servers,
            name=self._connection_name,
            no_echo=True,
            max_reconnect_attempts=-1,
            error_cb=self._on_error,
            disconnected_cb=self._on_disconnected,
            reconnected_cb=self._on_reconnected,
        )
        self._closing = False
        self._events.start()
        on_shutdown(self.disconnect)
        log.info("Connected to NATS as %s", self._connection_name)

    async def disconnect(self) -> None:
        """Finish in-flight work, then drain and close the connection

        Consumers finish messages they are processing, RPC handlers already running
        get to reply, and pending event messages are delivered before closing.
        Does nothing when not connected.
        """
        if self._client.is_closed or not self._events.started:
            return

        log.info("Disconnecting from NATS as %s", self._connection_name)
        self._closing = True
        for subject in list(self._consumers):
            await self.stop_consumer(subject)
        for subscription in self._rpc_subscriptions.values():
            with suppress(Exception):
                await subscription.drain()
        self._rpc_subscriptions.clear()
        if self._rpc_tasks:
            await asyncio.gather(*self._rpc_tasks, return_exceptions=True)
        self._subscriptions.clear()
        await self._client.drain()
        self._events.stop()
        log.info("Disconnected from NATS as %s", self._connection_name)

    def _check_connected(self) -> None:
        """Raise right away, subscribing before connect() would fail later in a confusing way"""
        if not self._events.started:
            msg = "Not connected to NATS"
            raise RuntimeError(msg)

    async def _on_error(self, error: Exception) -> None:
        log.error("NATS connection error: %s", error)

    async def _on_disconnected(self) -> None:
        if not self._closing:
            log.warning("Disconnected from NATS server, reconnecting...")

    async def _on_reconnected(self) -> None:
        log.info("Connection to NATS server reestablished: %s", self._client.connected_url)

    # --- events ---

    async def publish(self, subject: str, data: Any, *, headers: dict[str, str] | None = None) -> None:  # noqa: ANN401
        """Publish event, `bytes` are sent as they are, anything else is encoded as JSON"""
        payload = data if isinstance(data, bytes) else encoder.encode(data)
        await self._client.publish(subject, payload, headers=headers)

    async def subscribe(
        self,
        subject: str,
        handler: EventHandler,
        *,
        queue: str | None = None,
        raw: bool = False,
    ) -> None:
        """Call `handler(subject, data)` for every event on `subject`

        `subject` may contain wildcards (`*`, `>`). Events of all subscriptions are
        handled one by one in the order they arrived, so a handler awaiting slow I/O
        delays the others — start a task from it for such work. With `queue`, every
        event is delivered to only one subscriber of that queue group. With `raw`,
        `data` is the undecoded payload bytes — use it to forward messages without
        the cost of parsing them. A handler having a `headers` parameter (or `**kwargs`)
        also gets the event's NATS headers as `headers=` keyword argument.

        Several handlers may subscribe to the same subject, they share one NATS
        subscription and are called in the order they subscribed. The handler
        identifies the subscription for `unsubscribe()`.
        """
        self._check_connected()
        key = (subject, queue or "")
        options = HandlerOptions(raw=raw, with_headers=wants_headers(handler))
        async with self._subscribe_lock:
            entry = self._subscriptions.get(key)
            if entry is not None and handler in entry.handlers:
                msg = f'{handler!r} is already subscribed to "{subject}"'
                raise ValueError(msg)
            if entry is not None:
                entry.handlers[handler] = options
                return
            # without callback nats-py starts no task of its own, the shared event queue handles the messages
            subscription = await self._client.subscribe(subject, queue=queue or "")
            entry = EventSubscription(subscription)
            entry.handlers[handler] = options
            self._events.add(subscription, entry.on_message)
            self._subscriptions[key] = entry
            await self._sync_subscriptions()
            log.debug("Subscribed to NATS subject %s", subject)

    async def _sync_subscriptions(self) -> None:
        """Make sure the server registered all subscriptions made so far

        nats-py queues SUB for its flusher task but writes PING straight to the socket, so
        a single flush() sends PING before SUB and its PONG proves nothing. The first flush
        lets the flusher write the queued SUB, the second PING then follows it on the wire.
        """
        await self._client.flush()
        await self._client.flush()

    async def unsubscribe(self, subject: str, handler: EventHandler, *, queue: str | None = None) -> None:
        """Stop calling `handler` subscribed by `subscribe(subject, handler, queue=queue)`

        The NATS subscription is removed with its last handler. Does nothing after
        `disconnect()`, closing the connection removed all subscriptions already.
        """
        if self._client.is_closed:
            return
        key = (subject, queue or "")
        async with self._subscribe_lock:
            entry = self._subscriptions.get(key)
            if entry is None or entry.handlers.pop(handler, None) is None:
                log.warning('%r is not subscribed to NATS subject "%s"', handler, subject)
                return
            if entry.handlers:
                return
            del self._subscriptions[key]
            self._events.remove(entry.subscription)
            await entry.subscription.unsubscribe()
            log.debug("Unsubscribed from NATS subject %s", subject)

    # --- RPC ---

    async def request(self, subject: str, payload: bytes, *, timeout: float | None = None) -> bytes:
        """Send raw request and return raw reply payload"""
        msg = await self._client.request(subject, payload, timeout=self._rpc_timeout if timeout is None else timeout)
        return msg.data

    async def call(
        self,
        method: str,
        args: Sequence[Any] = (),
        kwargs: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> Any:  # noqa: ANN401
        """Call RPC `method` served by some service via `serve()` and return its result

        Raises `RpcError` when the remote handler failed or its reply isn't in the
        RPC wire format, and nats-py's `NoRespondersError`/`TimeoutError` when
        nobody serves the method or it didn't reply within `timeout` (default is
        `rpc_timeout` of the constructor).
        """
        payload = encoder.encode((args, kwargs or {}))
        return parse_reply(await self.request(RPC_PREFIX + method, payload, timeout=timeout))

    async def serve(self, method: str, handler: RpcHandler) -> None:
        """Handle RPC calls of `method` by `handler(*args, **kwargs)`

        The return value (JSON serializable) is sent back as the result. Calls run
        concurrently. All instances of this service share a queue group, so each call
        is handled by exactly one of them.
        """
        self._check_connected()
        subject = RPC_PREFIX + method
        if subject in self._rpc_subscriptions:
            msg = f'RPC method "{method}" is already served'
            raise ValueError(msg)

        async def on_request(msg: Msg) -> None:
            task = create_task(self._handle_rpc(msg, method, handler), f"RPC[{method}]")
            self._rpc_tasks.add(task)
            task.add_done_callback(self._rpc_tasks.discard)

        self._rpc_subscriptions[subject] = await self._client.subscribe(
            subject, queue=self._service_name, cb=on_request,
        )
        await self._sync_subscriptions()
        log.debug("Serving RPC method %s", method)

    async def _handle_rpc(self, msg: Msg, method: str, handler: RpcHandler) -> None:
        try:
            args, kwargs = parse_request(msg.data)
        except RpcError as error:
            log.warning("%s: %s", method, error.message)
            await self._reply(msg, error.code, error.message)
            return

        call = log_call(method, args, kwargs)
        try:
            result = await invoke(handler, *args, **kwargs)
        except RpcError as error:
            log.warning("%s -> RpcError(%s)", call, error.message)
            await self._reply(msg, error.code, error.message)
            return
        except Exception:  # noqa: BLE001 - reply instead of leaving the caller to time out
            log.trace("%s failed", call)
            # never leak internal details (paths, connection strings, ...) to the caller
            await self._reply(msg, 0, "Internal error")
            return

        log.debug("%s -> %s", call, short_repr(result))
        await self._reply(msg, RPC_OK, result)

    async def _reply(self, msg: Msg, code: int, result: Any) -> None:  # noqa: ANN401
        try:
            payload = encoder.encode((code, result))
        except TypeError:
            log.trace("RPC %s returned value that can't be encoded to JSON", msg.subject)
            payload = encoder.encode((0, "Internal error"))
        await msg.respond(payload)

    # --- JetStream ---

    async def consume(
        self,
        subject: str,
        handler: EventHandler,
        *,
        per_server: bool = False,
        concurrency: int = 1,
        ack_wait: float = 30.0,
    ) -> None:
        """Process JetStream messages on `subject` by `handler(subject, data)`

        Uses a durable consumer, so messages published while the service is down are
        processed once it's back. Like in `subscribe()`, a handler having a `headers`
        parameter also gets the message's NATS headers.

        :param per_server: When True, every server running this service processes
            each message. When False, each message is processed by only one of them.
        :param concurrency: How many messages are processed in parallel.
        :param ack_wait: Seconds before a message is redelivered to another consumer
            when this one dies while processing it. Long running handlers are kept
            alive automatically. When the handler raises, the message is NAKed and
            redelivered immediately.
        """
        self._check_connected()
        if subject in self._consumers:
            msg = f'Already consuming "{subject}"'
            raise ValueError(msg)

        name = self._connection_name if per_server else self._service_name
        durable = f"{_INVALID_DURABLE_CHARS.sub('_', name)}:{_INVALID_DURABLE_CHARS.sub('_', subject)}"
        stream = await self._js.find_stream_name_by_subject(subject)
        with suppress(js_errors.NotFoundError):
            info = await self._js.consumer_info(stream, durable)
            if info.config.ack_wait != ack_wait or info.config.filter_subject != subject:
                await self._js.delete_consumer(stream, durable)
                log.info("Recreating consumer %s because its configuration changed", durable)

        psub = await self._js.pull_subscribe(subject, durable, stream=stream, config=ConsumerConfig(ack_wait=ack_wait))
        consumer = Consumer(psub, handler, durable, ack_wait)
        self._consumers[subject] = consumer
        consumer.start(concurrency)
        log.info("Consumer %s processing %s from stream %s", durable, subject, stream)

    async def stop_consumer(self, subject: str) -> None:
        """Stop consuming `subject`, letting messages being processed finish

        Does nothing after `disconnect()`, it stopped all consumers already.
        """
        if self._client.is_closed:
            return
        consumer = self._consumers.pop(subject, None)
        if consumer is None:
            log.warning('Not consuming "%s"', subject)
            return
        await consumer.stop()
        log.info("Consumer %s stopped", consumer.durable)

    # These overloads exist purely so that omitting value_type/key_type infers
    # Bucket[str, Any] instead of leaving V/K unconstrained (which mypy resolves
    # to Never, rejecting every get()/put() call on the result) — the real
    # signature right below is what actually runs.
    @overload
    async def bucket(self, name: str, *, create: bool = True) -> Bucket[str, Any]: ...
    @overload
    async def bucket(self, name: str, *, value_type: type[V], create: bool = True) -> Bucket[str, V]: ...
    @overload
    async def bucket(self, name: str, *, key_type: type[K], create: bool = True) -> Bucket[K, Any]: ...
    @overload
    async def bucket(
        self, name: str, *, value_type: type[V], key_type: type[K], create: bool = True,
    ) -> Bucket[K, V]: ...

    async def bucket(
        self,
        name: str,
        *,
        value_type: type[V] | None = None,
        key_type: type[K] | None = None,
        create: bool = True,
    ) -> Bucket[K, V]:
        """Return key-value bucket `name`, creating it when it doesn't exist and `create` is True

        `value_type` decodes stored values as that type (e.g. a `msgspec.Struct`)
        instead of plain JSON; `key_type` likewise decodes keys as that type
        (e.g. a `str` `Enum`, `UUID`, or a plain `str` subtype) instead of a
        bare `str`. Both are only used the first time this bucket is opened —
        later calls for the same `name` return the cached instance as-is.
        """
        self._check_connected()
        bucket = self._buckets.get(name)
        if bucket is not None:
            return bucket

        try:
            kv = await self._js.key_value(name)
        except js_errors.BucketNotFoundError:
            if not create:
                raise
            kv = await self._js.create_key_value(bucket=name)
            log.info("Created key-value bucket %s", name)

        self._buckets[name] = bucket = Bucket(kv, value_type=value_type, key_type=key_type)
        return bucket

    async def delete_bucket(self, name: str) -> None:
        """Delete key-value bucket `name` with all its keys"""
        self._buckets.pop(name, None)
        await self._js.delete_key_value(name)
