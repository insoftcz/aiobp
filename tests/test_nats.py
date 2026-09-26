"""Integration tests for aiobp.nats against a real nats-server"""

import asyncio
import importlib
import logging
import shutil
import socket
import subprocess
import time
from collections.abc import AsyncIterator, Awaitable, Iterator
from enum import Enum
from typing import Any, Callable
from uuid import uuid4

import msgspec
import pytest
from nats.errors import NoRespondersError
from nats.errors import TimeoutError as NatsTimeoutError
from nats.js import errors as js_errors

from aiobp.nats import Bucket, BucketEntry, Nats, NatsConfig, RpcError

NATS_SERVER = shutil.which("nats-server")
# the aiobp package re-exports the runner() function under the module's name
_runner_module = importlib.import_module("aiobp.runner")

NatsFactory = Callable[..., Awaitable[Nats]]


class Item(msgspec.Struct):
    name: str
    price: float


class Room(str, Enum):
    LOBBY = "lobby"
    KITCHEN = "kitchen"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def unique(prefix: str) -> str:
    """Return a unique subject/stream/bucket name, tests share one server"""
    return f"{prefix}{uuid4().hex[:8]}"


async def wait_until(predicate: Callable[[], bool], timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            msg = "Condition not met in time"
            raise AssertionError(msg)
        await asyncio.sleep(0.01)


@pytest.fixture(scope="module")
def nats_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    if NATS_SERVER is None:
        pytest.skip("nats-server binary is not installed")

    port = _free_port()
    store = tmp_path_factory.mktemp("nats")
    process = subprocess.Popen(  # noqa: S603 - trusted binary found on PATH
        [NATS_SERVER, "-js", "-a", "127.0.0.1", "-p", str(port), "-sd", str(store)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 5
    while True:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.1).close()
            break
        except OSError:
            if process.poll() is not None or time.monotonic() > deadline:
                process.kill()
                pytest.fail("nats-server didn't start")
            time.sleep(0.05)

    yield f"nats://127.0.0.1:{port}"

    process.terminate()
    process.wait(timeout=5)


@pytest.fixture
async def make_nats(nats_url: str) -> AsyncIterator[NatsFactory]:
    """Create connected Nats instances, disconnected after the test"""
    created: list[Nats] = []

    async def factory(service_name: str = "svc", **kwargs: Any) -> Nats:  # noqa: ANN401
        nats = Nats(service_name, NatsConfig(url=nats_url), **kwargs)
        await nats.connect()
        created.append(nats)
        return nats

    yield factory

    for nats in created:
        await nats.disconnect()
    # connect() registers disconnect() for the runner's graceful shutdown, don't leak those between tests
    getattr(_runner_module, "__on_shutdown").clear()


@pytest.fixture
async def pair(make_nats: NatsFactory) -> tuple[Nats, Nats]:
    """Two connections — no_echo means a connection never receives its own messages"""
    return await make_nats("svc-a"), await make_nats("svc-b")


async def add_stream(nats: Nats) -> str:
    """Create a stream capturing `<prefix>.>` and return the prefix"""
    prefix = unique("jobs")
    await nats.jetstream.add_stream(name=prefix.upper(), subjects=[f"{prefix}.>"])
    return prefix


async def js_publish(nats: Nats, subject: str, data: Any) -> None:  # noqa: ANN401
    payload = data if isinstance(data, bytes) else msgspec.json.encode(data)
    await nats.jetstream.publish(subject, payload)


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------


class TestConnection:

    def test_multiple_servers_in_url(self) -> None:
        nats = Nats("svc", NatsConfig(url="nats://a:4222, nats://b:4222"))
        assert nats._servers == ["nats://a:4222", "nats://b:4222"]  # noqa: SLF001

    async def test_connected_property(self, nats_url: str) -> None:
        nats = Nats("svc", NatsConfig(url=nats_url))
        assert not nats.connected
        await nats.connect()
        assert nats.connected
        await nats.disconnect()
        assert not nats.connected
        getattr(_runner_module, "__on_shutdown").clear()

    async def test_connection_name_includes_location(self, make_nats: NatsFactory) -> None:
        nats = await make_nats("my-service", location="box.example.com")
        assert nats.client.options["name"] == "my-service@box.example.com"

    async def test_connect_registers_graceful_shutdown(self, make_nats: NatsFactory) -> None:
        nats = await make_nats()
        registered = [coroutine for coroutine, _args, _after in getattr(_runner_module, "__on_shutdown")]
        assert nats.disconnect in registered

    async def test_disconnect_twice_is_noop(self, make_nats: NatsFactory) -> None:
        nats = await make_nats()
        await nats.disconnect()
        await nats.disconnect()
        assert nats.client.is_closed

    async def test_intentional_disconnect_doesnt_warn_about_reconnecting(
        self, make_nats: NatsFactory, caplog: pytest.LogCaptureFixture,
    ) -> None:
        nats = await make_nats()
        with caplog.at_level(logging.WARNING):
            await nats.disconnect()
        assert "reconnecting" not in caplog.text


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


class TestEvents:

    async def test_publish_and_subscribe_json(self, pair: tuple[Nats, Nats]) -> None:
        a, b = pair
        subject = unique("ev.")
        received: list[tuple[str, Any]] = []
        await b.subscribe(subject, lambda s, d: received.append((s, d)))
        await a.publish(subject, {"x": 1})
        await wait_until(lambda: len(received) == 1)
        assert received == [(subject, {"x": 1})]

    async def test_async_handler(self, pair: tuple[Nats, Nats]) -> None:
        a, b = pair
        subject = unique("ev.")
        received: list[Any] = []

        async def handler(_subject: str, data: Any) -> None:  # noqa: ANN401
            await asyncio.sleep(0)
            received.append(data)

        await b.subscribe(subject, handler)
        await a.publish(subject, [1, 2])
        await wait_until(lambda: received == [[1, 2]])

    async def test_wildcard_subject_passes_actual_subject(self, pair: tuple[Nats, Nats]) -> None:
        a, b = pair
        prefix = unique("ev")
        received: list[str] = []
        await b.subscribe(f"{prefix}.>", lambda s, _d: received.append(s))
        await a.publish(f"{prefix}.one", 1)
        await a.publish(f"{prefix}.two.deep", 2)
        await wait_until(lambda: len(received) == 2)
        assert received == [f"{prefix}.one", f"{prefix}.two.deep"]

    async def test_struct_is_encoded(self, pair: tuple[Nats, Nats]) -> None:
        a, b = pair
        subject = unique("ev.")
        received: list[Any] = []
        await b.subscribe(subject, lambda _s, d: received.append(d))
        await a.publish(subject, Item("apple", 1.5))
        await wait_until(lambda: received == [{"name": "apple", "price": 1.5}])

    async def test_raw_subscription_and_bytes_passthrough(self, pair: tuple[Nats, Nats]) -> None:
        a, b = pair
        subject = unique("ev.")
        received: list[Any] = []
        await b.subscribe(subject, lambda _s, d: received.append(d), raw=True)
        await a.publish(subject, b"not even json")
        await wait_until(lambda: received == [b"not even json"])

    async def test_events_keep_order(self, pair: tuple[Nats, Nats]) -> None:
        a, b = pair
        subject = unique("ev.")
        received: list[int] = []

        async def handler(_subject: str, data: int) -> None:
            await asyncio.sleep(0)  # give later events a chance to overtake, they must not
            received.append(data)

        await b.subscribe(subject, handler)
        for i in range(100):
            await a.publish(subject, i)
        await wait_until(lambda: len(received) == 100)
        assert received == list(range(100))

    async def test_handler_exception_doesnt_kill_subscription(self, pair: tuple[Nats, Nats]) -> None:
        a, b = pair
        subject = unique("ev.")
        received: list[int] = []

        def handler(_subject: str, data: int) -> None:
            if data == 1:
                msg = "boom"
                raise ValueError(msg)
            received.append(data)

        await b.subscribe(subject, handler)
        for i in range(3):
            await a.publish(subject, i)
        await wait_until(lambda: received == [0, 2])

    async def test_invalid_json_event_is_skipped(self, pair: tuple[Nats, Nats]) -> None:
        a, b = pair
        subject = unique("ev.")
        received: list[Any] = []
        await b.subscribe(subject, lambda _s, d: received.append(d))
        await a.publish(subject, b"garbage")
        await a.publish(subject, {"ok": True})
        await wait_until(lambda: received == [{"ok": True}])

    async def test_duplicate_subscribe_raises(self, make_nats: NatsFactory) -> None:
        nats = await make_nats()
        subject = unique("ev.")
        await nats.subscribe(subject, lambda _s, _d: None)
        with pytest.raises(ValueError, match="Already subscribed"):
            await nats.subscribe(subject, lambda _s, _d: None)

    async def test_unsubscribe_stops_delivery(self, pair: tuple[Nats, Nats]) -> None:
        a, b = pair
        subject = unique("ev.")
        received: list[Any] = []
        await b.subscribe(subject, lambda _s, d: received.append(d))
        await b.unsubscribe(subject)
        await a.publish(subject, 1)
        await a.client.flush()
        await asyncio.sleep(0.1)
        assert received == []

    async def test_unsubscribe_unknown_subject_warns(
        self, make_nats: NatsFactory, caplog: pytest.LogCaptureFixture,
    ) -> None:
        nats = await make_nats()
        await nats.unsubscribe("never.subscribed")
        assert "Not subscribed" in caplog.text

    async def test_queue_group_delivers_each_event_once(self, make_nats: NatsFactory) -> None:
        publisher, first, second = await make_nats("pub"), await make_nats("w1"), await make_nats("w2")
        subject = unique("ev.")
        received: list[int] = []
        await first.subscribe(subject, lambda _s, d: received.append(d), queue="workers")
        await second.subscribe(subject, lambda _s, d: received.append(d), queue="workers")
        for i in range(20):
            await publisher.publish(subject, i)
        await wait_until(lambda: len(received) == 20)
        await asyncio.sleep(0.1)
        assert sorted(received) == list(range(20))


# ---------------------------------------------------------------------------
# RPC
# ---------------------------------------------------------------------------


class TestRpc:

    def test_rpc_error_code_can_not_mean_success(self) -> None:
        with pytest.raises(ValueError, match="means success"):
            RpcError("x", code=1)

    async def test_call_with_args_and_kwargs(self, pair: tuple[Nats, Nats]) -> None:
        a, b = pair
        method = unique("calc.add")

        async def add(x: int, y: int, *, scale: int = 1) -> int:
            return (x + y) * scale

        await b.serve(method, add)
        assert await a.call(method, (1, 2)) == 3
        assert await a.call(method, (1, 2), {"scale": 10}) == 30

    async def test_call_right_after_serve(self, pair: tuple[Nats, Nats]) -> None:
        a, b = pair
        for _ in range(20):
            method = unique("calc.echo")
            await b.serve(method, lambda x: x)
            assert await a.call(method, ("hi",)) == "hi"  # no NoRespondersError race

    async def test_event_right_after_subscribe(self, pair: tuple[Nats, Nats]) -> None:
        a, b = pair
        received: list[Any] = []
        for i in range(20):
            subject = unique("ev.")
            await b.subscribe(subject, lambda _s, d: received.append(d))
            await a.publish(subject, i)
        await wait_until(lambda: len(received) == 20)
        assert sorted(received) == list(range(20))

    async def test_sync_handler(self, pair: tuple[Nats, Nats]) -> None:
        a, b = pair
        method = unique("calc.double")
        await b.serve(method, lambda x: x * 2)
        assert await a.call(method, (21,)) == 42

    async def test_proxy_call(self, pair: tuple[Nats, Nats]) -> None:
        a, b = pair
        service = unique("calc")
        await b.serve(f"{service}.add", lambda x, y: x + y)
        assert await getattr(a.rpc, service).add(3, 4) == 7

    def test_proxy_doesnt_implement_dunder_protocols(self) -> None:
        nats = Nats("svc", NatsConfig())
        with pytest.raises(AttributeError):
            nats.rpc.__await__  # noqa: B018

    async def test_struct_result(self, pair: tuple[Nats, Nats]) -> None:
        a, b = pair
        method = unique("items.get")
        await b.serve(method, lambda: Item("pear", 2.0))
        assert await a.call(method) == {"name": "pear", "price": 2.0}

    async def test_rpc_error_is_propagated(self, pair: tuple[Nats, Nats]) -> None:
        a, b = pair
        method = unique("calc.fail")

        def fail() -> None:
            msg = "bad input"
            raise RpcError(msg, code=42)

        await b.serve(method, fail)
        with pytest.raises(RpcError) as error:
            await a.call(method)
        assert error.value.code == 42
        assert error.value.message == "bad input"

    async def test_unexpected_exception_doesnt_leak_details(self, pair: tuple[Nats, Nats]) -> None:
        a, b = pair
        method = unique("calc.boom")

        def boom() -> None:
            msg = "secret /etc/passwd"
            raise RuntimeError(msg)

        await b.serve(method, boom)
        with pytest.raises(RpcError) as error:
            await a.call(method)
        assert error.value.code == 0
        assert error.value.message == "Internal error"

    async def test_unencodable_result_is_internal_error(self, pair: tuple[Nats, Nats]) -> None:
        a, b = pair
        method = unique("calc.weird")
        await b.serve(method, object)
        with pytest.raises(RpcError, match="Internal error"):
            await a.call(method)

    async def test_no_responders(self, make_nats: NatsFactory) -> None:
        nats = await make_nats()
        with pytest.raises(NoRespondersError):
            await nats.call(unique("nobody.home"))

    async def test_timeout(self, pair: tuple[Nats, Nats]) -> None:
        a, b = pair
        method = unique("calc.slow")

        async def slow() -> None:
            await asyncio.sleep(0.5)

        await b.serve(method, slow)
        with pytest.raises(NatsTimeoutError):
            await a.call(method, timeout=0.1)

    @pytest.mark.parametrize(("payload", "error"), [
        (b"not json", "payload is not JSON"),
        (b'"nope"', "payload must be [args, kwargs]"),
        (b"[[1]]", "payload must be [args, kwargs]"),
        (b"[1, {}]", "args must be a list"),
        (b"[[], []]", "kwargs must be an object"),
    ])
    async def test_malformed_request(self, pair: tuple[Nats, Nats], payload: bytes, error: str) -> None:
        a, b = pair
        method = unique("calc.noop")
        await b.serve(method, lambda: None)
        code, message = msgspec.json.decode(await a.request(f"rpc.{method}", payload))
        assert code == 0
        assert error in message

    async def test_serve_twice_raises(self, make_nats: NatsFactory) -> None:
        nats = await make_nats()
        method = unique("calc.noop")
        await nats.serve(method, lambda: None)
        with pytest.raises(ValueError, match="already served"):
            await nats.serve(method, lambda: None)

    async def test_calls_run_concurrently(self, pair: tuple[Nats, Nats]) -> None:
        a, b = pair
        method = unique("calc.slow")

        async def slow() -> str:
            await asyncio.sleep(0.3)
            return "done"

        await b.serve(method, slow)
        start = time.monotonic()
        results = await asyncio.gather(*(a.call(method) for _ in range(5)))
        assert results == ["done"] * 5
        assert time.monotonic() - start < 1.0

    async def test_instances_of_one_service_share_calls(self, make_nats: NatsFactory) -> None:
        caller = await make_nats("caller")
        first = await make_nats("worker", location="one")
        second = await make_nats("worker", location="two")
        method = unique("work.do")
        handled: list[int] = []
        await first.serve(method, handled.append)
        await second.serve(method, handled.append)
        for i in range(20):
            await caller.call(method, (i,))
        assert sorted(handled) == list(range(20))  # each call handled by exactly one instance

    async def test_disconnect_waits_for_in_flight_call(self, pair: tuple[Nats, Nats]) -> None:
        a, b = pair
        method = unique("calc.slow")

        async def slow() -> str:
            await asyncio.sleep(0.3)
            return "done"

        await b.serve(method, slow)
        call = asyncio.create_task(a.call(method, timeout=3))
        await asyncio.sleep(0.1)
        await b.disconnect()
        assert await call == "done"


# ---------------------------------------------------------------------------
# JetStream consumers
# ---------------------------------------------------------------------------


class TestConsumers:

    async def test_messages_are_processed_and_acked(self, pair: tuple[Nats, Nats]) -> None:
        a, b = pair
        prefix = await add_stream(a)
        received: list[tuple[str, Any]] = []
        await b.consume(f"{prefix}.>", lambda s, d: received.append((s, d)))
        await js_publish(a, f"{prefix}.x", {"id": 1})
        await wait_until(lambda: len(received) == 1)
        assert received == [(f"{prefix}.x", {"id": 1})]

        await b.stop_consumer(f"{prefix}.>")
        info = await a.jetstream.consumer_info(prefix.upper(), f"svc-b:{prefix}__")
        assert info.num_ack_pending == 0
        assert info.num_pending == 0

    async def test_messages_published_before_consuming_are_processed(self, pair: tuple[Nats, Nats]) -> None:
        a, b = pair
        prefix = await add_stream(a)
        await js_publish(a, f"{prefix}.x", "early")
        received: list[Any] = []
        await b.consume(f"{prefix}.x", lambda _s, d: received.append(d))
        await wait_until(lambda: received == ["early"])

    async def test_failed_message_is_redelivered(self, pair: tuple[Nats, Nats]) -> None:
        a, b = pair
        prefix = await add_stream(a)
        attempts: list[Any] = []

        def handler(_subject: str, data: Any) -> None:  # noqa: ANN401
            attempts.append(data)
            if len(attempts) == 1:
                msg = "first attempt fails"
                raise ValueError(msg)

        await b.consume(f"{prefix}.x", handler, ack_wait=30)
        await js_publish(a, f"{prefix}.x", "job")
        # NAK redelivers right away, long before ack_wait would
        await wait_until(lambda: len(attempts) == 2)
        await asyncio.sleep(0.2)
        assert attempts == ["job", "job"]

    async def test_invalid_json_is_terminated_not_redelivered(self, pair: tuple[Nats, Nats]) -> None:
        a, b = pair
        prefix = await add_stream(a)
        received: list[Any] = []
        await b.consume(f"{prefix}.x", lambda _s, d: received.append(d), ack_wait=1)
        await js_publish(a, f"{prefix}.x", b"not json")
        await js_publish(a, f"{prefix}.x", "valid")
        await wait_until(lambda: received == ["valid"])
        await asyncio.sleep(1.5)  # past ack_wait, a merely unacked message would come back by now
        assert received == ["valid"]

    async def test_long_handler_is_kept_alive(self, pair: tuple[Nats, Nats]) -> None:
        a, b = pair
        prefix = await add_stream(a)
        attempts: list[Any] = []

        async def slow(_subject: str, data: Any) -> None:  # noqa: ANN401
            attempts.append(data)
            await asyncio.sleep(1.8)

        await b.consume(f"{prefix}.x", slow, ack_wait=1, concurrency=2)
        await js_publish(a, f"{prefix}.x", "job")
        await asyncio.sleep(2.5)
        assert attempts == ["job"]  # not redelivered to the idle second worker after ack_wait

    async def test_concurrency(self, pair: tuple[Nats, Nats]) -> None:
        a, b = pair
        prefix = await add_stream(a)
        done: list[Any] = []

        async def slow(_subject: str, data: Any) -> None:  # noqa: ANN401
            await asyncio.sleep(0.5)
            done.append(data)

        await b.consume(f"{prefix}.x", slow, concurrency=3)
        start = time.monotonic()
        for i in range(3):
            await js_publish(a, f"{prefix}.x", i)
        await wait_until(lambda: len(done) == 3)
        assert time.monotonic() - start < 1.2

    async def test_one_service_instance_processes_each_message(self, make_nats: NatsFactory) -> None:
        publisher = await make_nats("pub")
        first = await make_nats("worker", location="one")
        second = await make_nats("worker", location="two")
        prefix = await add_stream(publisher)
        received: list[int] = []
        await first.consume(f"{prefix}.x", lambda _s, d: received.append(d))
        await second.consume(f"{prefix}.x", lambda _s, d: received.append(d))
        for i in range(10):
            await js_publish(publisher, f"{prefix}.x", i)
        await wait_until(lambda: len(received) == 10)
        await asyncio.sleep(0.2)
        assert sorted(received) == list(range(10))

    async def test_per_server_processes_each_message_on_every_server(self, make_nats: NatsFactory) -> None:
        publisher = await make_nats("pub")
        first = await make_nats("worker", location="one.example.com")
        second = await make_nats("worker", location="two.example.com")
        prefix = await add_stream(publisher)
        received: list[int] = []
        await first.consume(f"{prefix}.x", lambda _s, d: received.append(d), per_server=True)
        await second.consume(f"{prefix}.x", lambda _s, d: received.append(d), per_server=True)
        for i in range(5):
            await js_publish(publisher, f"{prefix}.x", i)
        await wait_until(lambda: len(received) == 10)
        assert sorted(received) == sorted(list(range(5)) * 2)

        names = sorted(consumer.name for consumer in await publisher.jetstream.consumers_info(prefix.upper()))
        # dots of the hostname aren't allowed in consumer names
        assert names == [f"worker@one_example_com:{prefix}_x", f"worker@two_example_com:{prefix}_x"]

    async def test_changed_configuration_recreates_consumer(self, pair: tuple[Nats, Nats]) -> None:
        a, b = pair
        prefix = await add_stream(a)
        subject = f"{prefix}.x"
        durable = f"svc-b:{prefix}_x"
        await b.consume(subject, lambda _s, _d: None, ack_wait=5)
        await b.stop_consumer(subject)
        await b.consume(subject, lambda _s, _d: None, ack_wait=7)
        info = await a.jetstream.consumer_info(prefix.upper(), durable)
        assert info.config.ack_wait == 7

    async def test_stop_consumer_finishes_message_in_progress(self, pair: tuple[Nats, Nats]) -> None:
        a, b = pair
        prefix = await add_stream(a)
        started = asyncio.Event()
        finished: list[Any] = []

        async def slow(_subject: str, data: Any) -> None:  # noqa: ANN401
            started.set()
            await asyncio.sleep(0.3)
            finished.append(data)

        await b.consume(f"{prefix}.x", slow)
        await js_publish(a, f"{prefix}.x", "job")
        await asyncio.wait_for(started.wait(), timeout=3)
        await b.stop_consumer(f"{prefix}.x")
        assert finished == ["job"]
        info = await a.jetstream.consumer_info(prefix.upper(), f"svc-b:{prefix}_x")
        assert info.num_ack_pending == 0

    async def test_consume_twice_raises(self, make_nats: NatsFactory) -> None:
        nats = await make_nats()
        prefix = await add_stream(nats)
        await nats.consume(f"{prefix}.x", lambda _s, _d: None)
        with pytest.raises(ValueError, match="Already consuming"):
            await nats.consume(f"{prefix}.x", lambda _s, _d: None)

    async def test_stop_unknown_consumer_warns(self, make_nats: NatsFactory, caplog: pytest.LogCaptureFixture) -> None:
        nats = await make_nats()
        await nats.stop_consumer("never.consumed")
        assert "Not consuming" in caplog.text


# ---------------------------------------------------------------------------
# Key-value buckets
# ---------------------------------------------------------------------------


class TestBucket:

    async def test_put_get_delete(self, make_nats: NatsFactory) -> None:
        nats = await make_nats()
        bucket = await nats.bucket(unique("kv"))
        assert await bucket.keys() == []
        assert await bucket.get("missing") is None
        assert await bucket.get("missing", "default") == "default"

        await bucket.put("item", Item("pear", 2.0))
        assert await bucket.get("item") == {"name": "pear", "price": 2.0}
        assert await bucket.keys() == ["item"]

        await bucket.delete("item")
        assert await bucket.get("item") is None
        assert await bucket.keys() == []

    async def test_bucket_is_cached(self, make_nats: NatsFactory) -> None:
        nats = await make_nats()
        name = unique("kv")
        assert await nats.bucket(name) is await nats.bucket(name)

    async def test_existing_bucket_is_opened_not_recreated(self, pair: tuple[Nats, Nats]) -> None:
        a, b = pair
        name = unique("kv")
        await (await a.bucket(name)).put("key", 1)
        bucket = await b.bucket(name, create=False)
        assert isinstance(bucket, Bucket)
        assert await bucket.get("key") == 1

    async def test_missing_bucket_without_create_raises(self, make_nats: NatsFactory) -> None:
        nats = await make_nats()
        with pytest.raises(js_errors.BucketNotFoundError):
            await nats.bucket(unique("kv"), create=False)

    async def test_delete_bucket(self, make_nats: NatsFactory) -> None:
        nats = await make_nats()
        name = unique("kv")
        await nats.bucket(name)
        await nats.delete_bucket(name)
        with pytest.raises(js_errors.BucketNotFoundError):
            await nats.bucket(name, create=False)

    async def test_typed_bucket_decodes_into_struct(self, make_nats: NatsFactory) -> None:
        nats = await make_nats()
        bucket = await nats.bucket(unique("kv"), value_type=Item)
        await bucket.put("item", Item("pear", 2.0))
        value = await bucket.get("item")
        assert value == Item("pear", 2.0)
        assert isinstance(value, Item)

    async def test_typed_key_round_trips_through_str_enum(self, make_nats: NatsFactory) -> None:
        """Regression test: a str Enum's own __str__ ("Room.LOBBY") must never leak onto the wire."""
        nats = await make_nats()
        bucket = await nats.bucket(unique("kv"), key_type=Room)
        await bucket.put(Room.LOBBY, "sunny")

        assert await bucket.get(Room.LOBBY) == "sunny"
        assert await bucket.keys() == [Room.LOBBY]

    async def test_typed_key_round_trips_through_int(self, make_nats: NatsFactory) -> None:
        nats = await make_nats()
        bucket = await nats.bucket(unique("kv"), key_type=int)
        await bucket.put(42, "answer")

        assert await bucket.get(42) == "answer"
        keys = await bucket.keys()
        assert keys == [42]
        assert isinstance(keys[0], int)

    async def test_watch_yields_typed_key(self, make_nats: NatsFactory) -> None:
        nats = await make_nats()
        bucket = await nats.bucket(unique("kv"), key_type=Room)
        events: list[tuple[Room, Any, int]] = []

        async def collect() -> None:
            async for event in bucket.watch():
                events.append(event)
                return

        task = asyncio.create_task(collect())
        await asyncio.sleep(0.1)  # give the watch subscription time to start
        await bucket.put(Room.KITCHEN, "busy")

        await asyncio.wait_for(task, timeout=3)
        [(key, value, _revision)] = events
        assert key is Room.KITCHEN
        assert value == "busy"

    async def test_get_entry_returns_value_and_revision(self, make_nats: NatsFactory) -> None:
        nats = await make_nats()
        bucket = await nats.bucket(unique("kv"), value_type=Item)
        revision = await bucket.put("item", Item("pear", 2.0))
        entry = await bucket.get_entry("item")
        assert entry == BucketEntry(value=Item("pear", 2.0), revision=revision)

    async def test_get_entry_missing_key_returns_none(self, make_nats: NatsFactory) -> None:
        nats = await make_nats()
        bucket = await nats.bucket(unique("kv"))
        assert await bucket.get_entry("missing") is None

    async def test_update_with_correct_revision_succeeds(self, make_nats: NatsFactory) -> None:
        nats = await make_nats()
        bucket = await nats.bucket(unique("kv"), value_type=Item)
        revision = await bucket.put("item", Item("pear", 2.0))
        entry = BucketEntry(value=Item("pear", 3.0), revision=revision)
        new_revision = await bucket.update("item", entry)
        assert new_revision == revision + 1
        assert await bucket.get("item") == Item("pear", 3.0)

    async def test_update_with_stale_revision_raises(self, make_nats: NatsFactory) -> None:
        nats = await make_nats()
        bucket = await nats.bucket(unique("kv"), value_type=Item)
        revision = await bucket.put("item", Item("pear", 2.0))
        await bucket.update("item", BucketEntry(value=Item("pear", 3.0), revision=revision))
        with pytest.raises(js_errors.KeyWrongLastSequenceError):
            await bucket.update("item", BucketEntry(value=Item("pear", 4.0), revision=revision))

    async def test_get_entry_then_mutate_then_update_round_trip(self, make_nats: NatsFactory) -> None:
        """The realistic read-modify-write pattern: mutate entry.value in place, write the same entry back."""
        nats = await make_nats()
        bucket = await nats.bucket(unique("kv"), value_type=Item)
        await bucket.put("item", Item("pear", 2.0))

        entry = await bucket.get_entry("item")
        assert entry is not None
        entry.value.price = 3.0
        await bucket.update("item", entry)

        assert await bucket.get("item") == Item("pear", 3.0)

    async def test_watch_yields_put_and_delete_events(self, make_nats: NatsFactory) -> None:
        nats = await make_nats()
        bucket = await nats.bucket(unique("kv"), value_type=Item)
        events: list[tuple[str, Any, int]] = []

        async def collect() -> None:
            async for event in bucket.watch():
                events.append(event)
                if len(events) == 2:  # noqa: PLR2004
                    return

        task = asyncio.create_task(collect())
        await asyncio.sleep(0.1)  # give the watch subscription time to start
        await bucket.put("item", Item("pear", 2.0))
        await bucket.delete("item")

        await asyncio.wait_for(task, timeout=3)
        [put_event, delete_event] = events
        assert put_event == ("item", Item("pear", 2.0), 1)
        assert delete_event[0] == "item"
        assert delete_event[1] is None

    async def test_watch_stops_watcher_on_cancellation(self, make_nats: NatsFactory) -> None:
        nats = await make_nats()
        bucket = await nats.bucket(unique("kv"))
        started = asyncio.Event()

        async def run() -> None:
            started.set()
            async for _event in bucket.watch():
                pass

        task = asyncio.create_task(run())
        await asyncio.wait_for(started.wait(), timeout=3)
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # the watcher's subscription must be gone, or a fresh watch() would see it linger
        await bucket.put("after-cancel", 1)
        assert await bucket.get("after-cancel") == 1
