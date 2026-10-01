"""NATS message bus connector: events, RPC, JetStream consumers and key-value buckets"""

import asyncio
import inspect
import re
import reprlib
import socket
from collections.abc import AsyncIterator, Awaitable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Generic, Optional, TypeVar, Union, overload

import msgspec
from nats.aio.client import Client
from nats.aio.msg import Msg
from nats.errors import ConnectionClosedError
from nats.errors import TimeoutError as NatsTimeoutError
from nats.js import JetStreamContext
from nats.js import errors as js_errors
from nats.js.api import ConsumerConfig, DiscardPolicy, RetentionPolicy, StorageType, StreamConfig
from nats.js.kv import KeyValue

from aiobp.logging import log
from aiobp.runner import on_shutdown
from aiobp.task import create_task
from aiobp.tracing import context_from_headers, propagation_headers, traced, use_context

if TYPE_CHECKING:
    from nats.aio.subscription import Subscription

EventHandler = Callable[[str, Any], Union[Awaitable[None], None]]
RpcHandler = Callable[..., Any]
K = TypeVar("K")
V = TypeVar("V")

_RPC_PREFIX = "rpc."
_RPC_OK = 1
# How long a consumer worker waits in a single fetch() before polling again. Idle workers are
# cancelled on shutdown, so this doesn't delay it — it only bounds how stale a fetch can get.
_FETCH_TIMEOUT = 5.0
_FETCH_RETRY_DELAY = 1.0
# JetStream consumer names may not contain whitespace, ".", "*", ">" or path separators.
_INVALID_DURABLE_CHARS = re.compile(r"[\s.*>/\\]")
# How often keep_stream() re-checks its streams (besides after every reconnect).
_STREAM_CHECK_INTERVAL = 60.0

_encoder = msgspec.json.Encoder()
_decoder = msgspec.json.Decoder()
_repr = reprlib.Repr()
_repr.maxstring = 100
_repr.maxother = 100


@dataclass
class NatsConfig:
    """NATS server connection configuration

    `url` may list several cluster servers separated by commas.
    """

    url: str = "nats://127.0.0.1:4222"


class RpcError(Exception):
    """RPC call failed on the remote side

    Raise it from an RPC handler to reply with an error `message` (and optional
    `code`) without logging a traceback — use it for expected failures such as
    invalid arguments. `Nats.call()` raises it when the remote handler failed.
    """

    def __init__(self, message: str, code: int = 0) -> None:
        if code == _RPC_OK:
            msg = f"RPC error code can't be {_RPC_OK}, it means success"
            raise ValueError(msg)
        super().__init__(message)
        self.message: str = message
        self.code: int = code


@dataclass
class BucketEntry(Generic[V]):
    """A bucket value together with its revision, for `Bucket.update()`'s optimistic concurrency check"""

    value: V
    revision: Optional[int]


class Bucket(Generic[K, V]):
    """JetStream key-value bucket with JSON encoded values of type `V`, keyed by `K`

    The underlying nats-py `KeyValue` is available as `kv` for anything not covered here.
    """

    def __init__(
        self, kv: KeyValue, *, value_type: Optional[type[V]] = None, key_type: Optional[type[K]] = None,
    ) -> None:
        self.kv: KeyValue = kv
        self._decoder: msgspec.json.Decoder = msgspec.json.Decoder(type=value_type if value_type is not None else Any)
        self._key_type: type[K] = key_type if key_type is not None else str  # type: ignore[assignment]

    def _encode_key(self, key: K) -> str:
        # str.__str__, not str(key): a str subclass (e.g. a str Enum) can override __str__ to
        # return something other than its own value ("Room.LOBBY" instead of "lobby") — nats-py
        # builds the wire subject via string formatting, so that override would leak onto the wire.
        return str.__str__(key) if isinstance(key, str) else str(key)

    def _decode_key(self, key: str) -> K:
        return msgspec.convert(key, type=self._key_type, strict=False)

    async def get(self, key: K, default: Optional[V] = None) -> Optional[V]:
        """Return value stored under `key`, or `default` when there is none"""
        try:
            entry = await self.kv.get(self._encode_key(key))
        except js_errors.KeyNotFoundError:
            return default
        if entry.value is None:
            return default
        return self._decoder.decode(entry.value)

    async def get_entry(self, key: K) -> Optional[BucketEntry[V]]:
        """Return `key`'s value together with its revision, or None when there is none

        Use this instead of `get()` when you intend to `update()` the value
        afterwards — `update()` needs the revision to detect a concurrent change.
        """
        try:
            entry = await self.kv.get(self._encode_key(key))
        except js_errors.KeyNotFoundError:
            return None
        if entry.value is None:
            return None
        assert entry.revision is not None  # noqa: S101 - always set on a real entry, narrows for mypy
        return BucketEntry(value=self._decoder.decode(entry.value), revision=entry.revision)

    async def put(self, key: K, value: V) -> int:
        """Store `value` under `key` and return the new revision"""
        return await self.kv.put(self._encode_key(key), _encoder.encode(value))

    async def update(self, key: K, entry: BucketEntry[V]) -> int:
        """Store `entry.value` under `key`, but only if it's still at `entry.revision`; return the new revision

        `entry` is normally the one you got from `get_entry()`, its `value`
        mutated in place — that's what couples the write to the read it's
        based on. Raises nats-py's `KeyWrongLastSequenceError` if `key` was
        changed by someone else since `entry.revision` was read — call
        `get_entry()` again and retry your change against the fresh value.
        """
        return await self.kv.update(self._encode_key(key), _encoder.encode(entry.value), last=entry.revision)

    async def delete(self, key: K) -> None:
        """Remove `key` from the bucket"""
        await self.kv.delete(self._encode_key(key))

    async def keys(self) -> list[K]:
        """List keys in the bucket"""
        try:
            raw_keys = await self.kv.keys()
        except js_errors.NoKeysError:
            return []
        return [self._decode_key(key) for key in raw_keys]

    async def watch(
        self,
        keys: str = ">",
        *,
        include_history: bool = False,
    ) -> AsyncIterator[tuple[K, Optional[V], int]]:
        """Async-iterate over `(key, value, revision)` for every change matching `keys`

        `keys` is a raw NATS subject pattern (e.g. `"user.*"`), not a `K` — it
        may contain wildcards, which a single key never does. `value` is None
        when the change is a deletion. Runs until the caller stops iterating
        (e.g. its task is cancelled) or the server closes the watch
        subscription; either way, the underlying watcher is stopped properly
        before this returns.
        """
        watcher = await self.kv.watch(keys, include_history=include_history)
        try:
            async for entry in watcher:
                if entry is None:  # internal marker: history replay caught up, not a real change
                    continue
                # a deletion carries operation="DEL" with an *empty* (not None) value
                value = self._decoder.decode(entry.value) if entry.value else None
                yield self._decode_key(entry.key), value, entry.revision
        finally:
            await watcher.stop()


async def _invoke(handler: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
    """Call handler, awaiting it when it is a coroutine function"""
    result = handler(*args, **kwargs)
    if inspect.isawaitable(result):
        result = await result
    return result


def _log_call(method: str, args: Sequence[Any], kwargs: dict[str, Any]) -> str:
    """Format RPC call for logging, truncating large arguments"""
    formatted = [_repr.repr(arg) for arg in args]
    formatted.extend(f"{key}={_repr.repr(value)}" for key, value in kwargs.items())
    return f"{method}({', '.join(formatted)})"


def _parse_rpc_request(payload: bytes) -> tuple[list[Any], dict[str, Any]]:
    """Decode `[args, kwargs]` RPC request payload"""
    try:
        data = _decoder.decode(payload)
    except msgspec.DecodeError as error:
        msg = f"Invalid RPC request, payload is not JSON: {error}"
        raise RpcError(msg) from error
    if not isinstance(data, list) or len(data) != 2:  # noqa: PLR2004
        msg = "Invalid RPC request, payload must be [args, kwargs]"
        raise RpcError(msg)
    args, kwargs = data
    if not isinstance(args, list):
        msg = "Invalid RPC request, args must be a list"
        raise RpcError(msg)
    if not isinstance(kwargs, dict):
        msg = "Invalid RPC request, kwargs must be an object"
        raise RpcError(msg)
    return args, kwargs


class _RpcProxy:
    """Turn `nats.rpc.ami.originate(...)` into `nats.call("ami.originate", ...)`"""

    __slots__ = ("_nats", "_path")

    def __init__(self, nats: "Nats", path: tuple[str, ...]) -> None:
        self._nats = nats
        self._path = path

    def __getattr__(self, name: str) -> "_RpcProxy":
        if name.startswith("__"):  # don't pretend to implement protocols like __await__
            raise AttributeError(name)
        return _RpcProxy(self._nats, (*self._path, name))

    def __call__(self, *args: Any, **kwargs: Any) -> Awaitable[Any]:  # noqa: ANN401
        return self._nats.call(".".join(self._path), args, kwargs)


class _Consumer:
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
        self.durable = durable
        self.ack_wait = ack_wait
        self.stopping = False
        self.workers: list[asyncio.Task] = []
        self.idle: set[asyncio.Task] = set()  # workers waiting in fetch(), safe to cancel

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
        try:
            data = _decoder.decode(msg.data)
        except msgspec.DecodeError:
            # redelivery can't fix a malformed message, drop it for good
            log.error("Consumer %s got invalid JSON on %s, terminating: %r", self.durable, msg.subject, msg.data[:200])
            await msg.term()
            return

        log.debug("Consumer %s processing %s", self.durable, msg.subject)
        keepalive = create_task(self._keepalive(msg), f"Keepalive[{msg.subject}]")
        # let the keepalive task start, cancelling it before its first step would leave its coroutine never awaited
        await asyncio.sleep(0)
        try:
            with use_context(context_from_headers(msg.headers)):
                await _invoke(self.handler, msg.subject, data)
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


class Nats:
    """NATS message bus connector

    Payloads are JSON (msgspec, so `msgspec.Struct` works too). The connection
    reconnects forever, never receives its own published messages (`no_echo`)
    and is drained on graceful shutdown.

    - events: `publish()` / `subscribe()`
    - RPC: `call()` or `rpc.<method>(...)` to call, `serve()` to handle calls;
      wire format is request `[args, kwargs]` and reply `[1, result]` on success
      or `[code, message]` on failure, sent to subject `rpc.<method>`
    - JetStream: `consume()` for durable work queues, `bucket()` for key-value
      storage, `keep_stream()` to own a stream

    Tracing: everything sent carries the current trace in a `traceparent`
    header, and every handler (events, RPC, consumers) runs inside the trace
    of the message it handles — so one trace follows a request across services.

    `client` and `jetstream` expose the underlying nats-py objects for anything else.
    """

    def __init__(
        self,
        service_name: str,
        config: NatsConfig,
        *,
        location: Optional[str] = None,
        rpc_timeout: float = 2.0,
    ) -> None:
        self._service_name: str = service_name
        self._connection_name: str = f"{service_name}@{location or socket.getfqdn()}"
        self._servers: list[str] = [url.strip() for url in config.url.split(",")]
        self._rpc_timeout: float = rpc_timeout
        self._client: Client = Client()
        self._js: JetStreamContext = self._client.jetstream()
        self._subscriptions: dict[str, Subscription] = {}
        self._rpc_subscriptions: dict[str, Subscription] = {}
        self._rpc_tasks: set[asyncio.Task] = set()
        self._consumers: dict[str, _Consumer] = {}
        self._buckets: dict[str, Bucket] = {}
        self._streams: dict[str, StreamConfig] = {}  # kept by keep_stream()
        self._stream_keeper: Optional[asyncio.Task] = None
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
    def rpc(self) -> _RpcProxy:
        """Call RPC methods as attributes: `await nats.rpc.ami.originate(ext, number=n)`"""
        return _RpcProxy(self, ())

    async def connect(self) -> None:
        """Connect to NATS server and disconnect gracefully on shutdown"""
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
        on_shutdown(self.disconnect)
        log.info("Connected to NATS as %s", self._connection_name)

    async def disconnect(self) -> None:
        """Finish in-flight work, then drain and close the connection

        Consumers finish messages they are processing, RPC handlers already running
        get to reply, and pending event messages are delivered before closing.
        """
        if self._client.is_closed:
            return

        log.info("Disconnecting from NATS as %s", self._connection_name)
        self._closing = True
        if self._stream_keeper is not None:
            self._stream_keeper.cancel()
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
        log.info("Disconnected from NATS as %s", self._connection_name)

    async def _on_error(self, error: Exception) -> None:
        log.error("NATS connection error: %s", error)

    async def _on_disconnected(self) -> None:
        if not self._closing:
            log.warning("Disconnected from NATS server, reconnecting...")

    async def _on_reconnected(self) -> None:
        log.info("Connection to NATS server reestablished: %s", self._client.connected_url)
        if self._streams:
            # the server may have lost them (restart without storage, manual delete)
            create_task(self._check_streams(), "NATS streams check")

    # --- events ---

    async def publish(self, subject: str, data: Any) -> None:  # noqa: ANN401
        """Publish event, `bytes` are sent as they are, anything else is encoded as JSON"""
        payload = data if isinstance(data, bytes) else _encoder.encode(data)
        await self._client.publish(subject, payload, headers=propagation_headers() or None)

    async def subscribe(
        self,
        subject: str,
        handler: EventHandler,
        *,
        queue: Optional[str] = None,
        raw: bool = False,
    ) -> None:
        """Call `handler(subject, data)` for every event on `subject`

        `subject` may contain wildcards (`*`, `>`). Events of one subscription are
        handled one by one in the order they arrived. With `queue`, every event is
        delivered to only one subscriber of that queue group. With `raw`, `data` is
        the undecoded payload bytes — use it to forward messages without the cost
        of parsing them.
        """
        if subject in self._subscriptions:
            msg = f'Already subscribed to "{subject}"'
            raise ValueError(msg)

        async def on_message(msg: Msg) -> None:
            if raw:
                data: Any = msg.data
            else:
                try:
                    data = _decoder.decode(msg.data)
                except msgspec.DecodeError:
                    log.warning("Invalid JSON event on %s: %r", msg.subject, msg.data[:200])
                    return
            try:
                with use_context(context_from_headers(msg.headers)):
                    await _invoke(handler, msg.subject, data)
            except Exception:  # noqa: BLE001 - one bad event must not kill the subscription
                log.trace('Error handling NATS event "%s"', msg.subject)

        self._subscriptions[subject] = await self._client.subscribe(subject, queue=queue or "", cb=on_message)
        # the subscription is sent to the server asynchronously, make sure it's active once we return
        await self._client.flush()
        log.debug("Subscribed to NATS subject %s", subject)

    async def unsubscribe(self, subject: str) -> None:
        """Stop receiving events subscribed by `subscribe(subject, ...)`"""
        subscription = self._subscriptions.pop(subject, None)
        if subscription is None:
            log.warning('Not subscribed to NATS subject "%s"', subject)
            return
        await subscription.unsubscribe()
        log.debug("Unsubscribed from NATS subject %s", subject)

    # --- RPC ---

    async def request(self, subject: str, payload: bytes, *, timeout: Optional[float] = None) -> bytes:
        """Send raw request and return raw reply payload"""
        msg = await self._client.request(
            subject, payload,
            timeout=self._rpc_timeout if timeout is None else timeout,
            headers=propagation_headers() or None,
        )
        return msg.data

    async def call(
        self,
        method: str,
        args: Sequence[Any] = (),
        kwargs: Optional[dict[str, Any]] = None,
        *,
        timeout: Optional[float] = None,
    ) -> Any:  # noqa: ANN401
        """Call RPC `method` served by some service via `serve()` and return its result

        Raises `RpcError` when the remote handler failed, and nats-py's
        `NoRespondersError`/`TimeoutError` when nobody serves the method or it
        didn't reply within `timeout` (default is `rpc_timeout` of the constructor).
        """
        payload = _encoder.encode((args, kwargs or {}))
        reply = await self.request(_RPC_PREFIX + method, payload, timeout=timeout)
        status, result = _decoder.decode(reply)
        if status != _RPC_OK:
            raise RpcError(result, code=status)
        return result

    async def serve(self, method: str, handler: RpcHandler) -> None:
        """Handle RPC calls of `method` by `handler(*args, **kwargs)`

        The return value (JSON serializable) is sent back as the result. Calls run
        concurrently. All instances of this service share a queue group, so each call
        is handled by exactly one of them.
        """
        subject = _RPC_PREFIX + method
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
        # the subscription is sent to the server asynchronously, make sure calls reach us once we return
        await self._client.flush()
        log.debug("Serving RPC method %s", method)

    async def _handle_rpc(self, msg: Msg, method: str, handler: RpcHandler) -> None:
        try:
            args, kwargs = _parse_rpc_request(msg.data)
        except RpcError as error:
            log.warning("%s: %s", method, error.message)
            await self._reply(msg, error.code, error.message)
            return

        call = _log_call(method, args, kwargs)
        try:
            async with traced(f"RPC {method}", context=context_from_headers(msg.headers)):
                result = await _invoke(handler, *args, **kwargs)
        except RpcError as error:
            log.warning("%s -> RpcError(%s)", call, error.message)
            await self._reply(msg, error.code, error.message)
            return
        except Exception:  # noqa: BLE001 - reply instead of leaving the caller to time out
            log.trace("%s failed", call)
            # never leak internal details (paths, connection strings, ...) to the caller
            await self._reply(msg, 0, "Internal error")
            return

        log.debug("%s -> %s", call, _repr.repr(result))
        await self._reply(msg, _RPC_OK, result)

    async def _reply(self, msg: Msg, code: int, result: Any) -> None:  # noqa: ANN401
        try:
            payload = _encoder.encode((code, result))
        except TypeError:
            log.trace("RPC %s returned value that can't be encoded to JSON", msg.subject)
            payload = _encoder.encode((0, "Internal error"))
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
        processed once it's back.

        :param per_server: When True, every server running this service processes
            each message. When False, each message is processed by only one of them.
        :param concurrency: How many messages are processed in parallel.
        :param ack_wait: Seconds before a message is redelivered to another consumer
            when this one dies while processing it. Long running handlers are kept
            alive automatically. When the handler raises, the message is NAKed and
            redelivered immediately.
        """
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
        consumer = _Consumer(psub, handler, durable, ack_wait)
        self._consumers[subject] = consumer
        consumer.start(concurrency)
        log.info("Consumer %s processing %s from stream %s", durable, subject, stream)

    async def keep_stream(
        self,
        name: str,
        subjects: Sequence[str],
        *,
        max_age: Optional[float] = None,
        max_bytes: Optional[int] = None,
    ) -> None:
        """Own JetStream stream `name`: create it, and keep it existing with this configuration

        The stream is created or updated now, again after every reconnect and
        every minute, so a deleted or changed stream is repaired. Retention is by
        limits: messages stay and can be read any number of times until they are
        older than `max_age` seconds or `max_bytes` pushes out the oldest.
        """
        config = StreamConfig(
            name=name,
            subjects=list(subjects),
            retention=RetentionPolicy.LIMITS,
            discard=DiscardPolicy.OLD,
            storage=StorageType.FILE,
            max_age=max_age,
            max_bytes=max_bytes,
        )
        self._streams[name] = config
        await self._ensure_stream(config)
        if self._stream_keeper is None:
            self._stream_keeper = create_task(self._keep_streams(), "NATS streams keeper")

    async def _keep_streams(self) -> None:
        while True:
            await asyncio.sleep(_STREAM_CHECK_INTERVAL)
            await self._check_streams()

    async def _check_streams(self) -> None:
        for config in list(self._streams.values()):
            try:
                await self._ensure_stream(config)
            except Exception:  # noqa: BLE001 - try again at the next check
                log.trace("Keeping stream %s failed", config.name)

    async def _ensure_stream(self, config: StreamConfig) -> None:
        assert config.name is not None  # noqa: S101 - keep_stream() always sets it, narrows for mypy
        try:
            info = await self._js.stream_info(config.name)
        except js_errors.NotFoundError:
            await self._js.add_stream(config)
            log.info("Created stream %s for %s", config.name, ", ".join(config.subjects or []))
            return
        current = info.config
        if (
            sorted(current.subjects or []) != sorted(config.subjects or [])
            or (current.max_age or 0) != (config.max_age or 0)
            or (current.max_bytes or -1) != (config.max_bytes or -1)
        ):
            await self._js.update_stream(config)
            log.info("Updated stream %s to its configured subjects and limits", config.name)

    async def stop_consumer(self, subject: str) -> None:
        """Stop consuming `subject`, letting messages being processed finish"""
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
    async def bucket(self, name: str, *, create: bool = True) -> "Bucket[str, Any]": ...
    @overload
    async def bucket(self, name: str, *, value_type: type[V], create: bool = True) -> "Bucket[str, V]": ...
    @overload
    async def bucket(self, name: str, *, key_type: type[K], create: bool = True) -> "Bucket[K, Any]": ...
    @overload
    async def bucket(
        self, name: str, *, value_type: type[V], key_type: type[K], create: bool = True,
    ) -> "Bucket[K, V]": ...

    async def bucket(
        self,
        name: str,
        *,
        value_type: Optional[type[V]] = None,
        key_type: Optional[type[K]] = None,
        create: bool = True,
    ) -> Bucket[K, V]:
        """Return key-value bucket `name`, creating it when it doesn't exist and `create` is True

        `value_type` decodes stored values as that type (e.g. a `msgspec.Struct`)
        instead of plain JSON; `key_type` likewise decodes keys as that type
        (e.g. a `str` `Enum`, `UUID`, or a plain `str` subtype) instead of a
        bare `str`. Both are only used the first time this bucket is opened —
        later calls for the same `name` return the cached instance as-is.
        """
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
