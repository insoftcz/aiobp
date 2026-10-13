NATS Message Bus Connector
==========================

One connector for everything our services do over NATS: events, RPC between
services, durable JetStream work queues and key-value storage. Payloads are
JSON encoded by [msgspec](https://msgspec.dev/), so plain Python
values and `msgspec.Struct` instances can be sent as they are.

```bash
pip install aiobp[nats]
```

Quick start
-----------

```python
import msgspec

from aiobp import runner
from aiobp.nats import Nats, NatsConfig, RpcError


class Company(msgspec.Struct):
    id: int
    name: str


COMPANIES = {1: Company(1, "Acme Ltd.")}


async def company_get(company_id: int) -> Company:
    company = COMPANIES.get(company_id)
    if company is None:
        raise RpcError(f"Company {company_id} not found", code=404)
    return company


async def on_peer_change(subject: str, peer: dict) -> None:
    print(f"{subject}: {peer['name']} status {peer['status']}")


async def send_invoice(subject: str, invoice: dict) -> None:
    ...  # raising here makes JetStream redeliver the message


async def main() -> None:
    nats = Nats("ucs-crm", NatsConfig(url="nats://127.0.0.1:4222"))
    await nats.connect()  # disconnects gracefully when the runner shuts down

    await nats.serve("crm.company_get", company_get)
    await nats.subscribe("peer.change", on_peer_change)
    await nats.consume("invoices.send", send_invoice, concurrency=4)

    await nats.publish("crm.started", {"companies": len(COMPANIES)})


runner("ucs-crm", "1.0.0", main())
```

Another service then calls the RPC method:

```python
company = await nats.call("crm.company_get", (1,))
company = await nats.rpc.crm.company_get(1)  # the same, as attribute access
```

Configuration
-------------

`NatsConfig` is a dataclass, so it plugs into `aiobp.config` like any other
section. `url` may list several servers of a cluster separated by commas:

```ini
[nats]
url = nats://nats1:4222,nats://nats2:4222
```

`Nats(service_name, config, *, location=None, rpc_timeout=2.0)`:

- `service_name` identifies the service. The connection is named
  `<service_name>@<location>`, where `location` defaults to the machine's FQDN.
  All instances of one service share RPC calls and JetStream messages (see below).
- `rpc_timeout` is the default timeout of `call()` and `request()` in seconds.

The connection reconnects forever and never receives messages it published
itself (`no_echo`). A service therefore can't `call()` an RPC method it serves.

Events
------

```python
await nats.publish("peer.change", {"name": "101", "status": 1})
await nats.publish("peer.change", raw_bytes)  # bytes are sent unchanged
await nats.publish("peer.change", data, headers={"X-Groups": "[1, 8]"})

await nats.subscribe("peer.change", handler)       # handler(subject, data)
await nats.subscribe("peer.>", handler)            # wildcards work
await nats.subscribe("jobs", handler, queue="w")   # one subscriber of queue "w" gets each event
await nats.subscribe("peer.>", handler, raw=True)  # data is undecoded bytes
await nats.unsubscribe("peer.>", handler)          # the handler identifies the subscription
```

Handlers may be plain functions or coroutines. An exception in a handler is
logged and the next event is handled normally.

A handler that needs the event's NATS headers declares a parameter named
`headers`, it gets them as a dict, empty when the event has none:

```python
def on_change(subject: str, data: dict, headers: dict[str, str]) -> None: ...
def on_change(subject: str, data: dict, *, headers: dict[str, str]) -> None: ...  # also fine
```

The signature is checked once when subscribing, `headers` is passed as a keyword
argument only to handlers having a parameter of exactly that name (or
`**kwargs`). The same applies to JetStream consumers.

Several handlers may subscribe to the same subject — e.g. independent components
of one service. They share one NATS subscription per subject and queue group,
the payload is decoded once and handlers are called one by one in the order they
subscribed. The same handler can't subscribe to the same subject twice.

`unsubscribe()` needs the same handler (and `queue`) that subscribed, the NATS
subscription is removed with its last handler. Bound methods work as expected,
`unsubscribe(subject, self.on_event)` matches `subscribe(subject, self.on_event)`.
A lambda can only be unsubscribed when you keep a reference to it.

Use `raw=True` to forward events without paying for decoding them, e.g.:

```python
def forward(subject: str, data: bytes, headers: dict[str, str]) -> None:
    frame = b"".join((b'{"method":"', subject.encode(), b'","params":[', data, b"]}"))
    websocket_broadcast(frame)

await nats.subscribe(">", forward, raw=True)
```

### Ordering

Events of all subscriptions are handled one by one in the order they arrived,
even across separate subscriptions: when a service publishes `call.new` and
then `call.answer`, `call.answer` is handled only after the `call.new` handler
finished. NATS keeps the order of events from one publisher, events from
different publishers have no defined order.

A handler that awaits slow I/O (database, HTTP) delays all other events until
it finishes — start a task from the handler for such work. CPU heavy work blocks
the whole event loop no matter how events are handled, run it in a thread
(awaiting the thread still delays other events, wrap it in a task if it shouldn't):

```python
async def on_new(subject: str, data: dict) -> None:
    calls[data["id"]] = data                        # fast, keeps the order
    create_task(fetch_caller_info(data), "caller")  # slow I/O, runs concurrently
    await asyncio.to_thread(parse_recording, data)  # CPU heavy, loop stays free
```

`create_task` comes from `aiobp.task`.

RPC
---

```python
async def originate(extension: str, number: str, *, timeout: int = 30) -> str:
    ...
    return call_id

await nats.serve("ami.originate", originate)
```

The handler is called with the caller's `*args, **kwargs` and its return value
is sent back. Calls are handled concurrently. When several instances of the same
service serve a method, each call goes to exactly one of them.

```python
call_id = await nats.call("ami.originate", ("101", "+420123456789"), {"timeout": 10})
call_id = await nats.rpc.ami.originate("101", "+420123456789", timeout=10)
call_id = await nats.call("ami.originate", ("101", "+420123456789"), timeout=5.0)  # RPC timeout
```

`call()` accepts `timeout=` next to the arguments, the attribute form always uses
the default `rpc_timeout`.

Errors:

- Raise `RpcError(message, code=...)` in a handler for expected failures. The
  caller gets `RpcError` with the same `message` and `code`, nothing is logged
  as a traceback.
- Any other exception is logged with traceback and the caller gets
  `RpcError("Internal error", code=0)`. Internal details never leave the service.
- When nobody serves the method, `call()` raises `nats.errors.NoRespondersError`.
  When the handler doesn't reply in time, it raises `nats.errors.TimeoutError`.
- A reply that isn't in the RPC wire format (e.g. from a non-aiobp responder)
  raises `RpcError("Invalid RPC reply: ...", code=0)`.

Wire format, for services in other languages: the request is sent to subject
`rpc.<method>` with payload `[args, kwargs]`; the reply is `[1, result]` on
success or `[code, message]` on failure.

`request(subject, payload, timeout=None)` sends a raw request and returns the
raw reply bytes, for subjects that don't use the RPC format.

JetStream consumers
-------------------

Messages published to a JetStream stream are processed by a durable consumer,
so messages published while the service was down are processed when it's back.
The stream has to exist already.

```python
async def send_invoice(subject: str, invoice: dict) -> None:
    await mailer.send(invoice)

await nats.consume("invoices.send", send_invoice)
await nats.consume("cache.flush", flush, per_server=True)
await nats.consume("reports.>", build_report, concurrency=4, ack_wait=60.0)
await nats.stop_consumer("reports.>")
```

- A message is acknowledged when the handler returns. When it raises, the
  message is NAKed and redelivered right away.
- `per_server=False` (default): each message is processed by one instance of the
  service. `per_server=True`: each message is processed on every server.
- `concurrency`: how many messages are processed in parallel.
- `ack_wait`: seconds before a message is handed to another consumer when this
  one died while processing it. Handlers running longer are kept alive
  automatically, you don't have to size `ack_wait` for your slowest message.
- Messages that aren't valid JSON are dropped for good (terminated), because
  redelivering them can't help.
- When `ack_wait` or the subject of an existing consumer changes, the consumer
  is recreated.

Key-value buckets
-----------------

```python
settings = await nats.bucket("settings")  # created when it doesn't exist
await settings.put("theme", {"color": "dark"})
await settings.get("theme")               # {"color": "dark"}
await settings.get("missing", "default")  # "default"
await settings.keys()                     # ["theme"]
await settings.delete("theme")

await nats.bucket("settings", create=False)  # raises BucketNotFoundError if missing
await nats.delete_bucket("settings")
```

Values are stored as JSON. `bucket.kv` is the underlying nats-py `KeyValue`
for anything else (watching, history, revisions).

Shutdown
--------

`connect()` registers `disconnect()` with the runner's graceful shutdown. You
can also call it yourself, calling it again does nothing. It:

1. stops consumers, letting messages being processed finish and get acknowledged,
2. stops accepting RPC calls and waits for calls already running to reply,
3. delivers event messages already received and closes the connection.

The runner calls it before it cancels tasks, so tasks can't use NATS while
they are being cancelled: `publish()`, `call()` and the like raise nats-py's
`ConnectionClosedError` then. Cleanup that only releases NATS resources is
safe — `unsubscribe()` and `stop_consumer()` do nothing after `disconnect()`
and `Bucket.watch()` ends with the task's `CancelledError`.

Everything else
---------------

`nats.client` is the underlying nats-py `Client` and `nats.jetstream` its
`JetStreamContext`, e.g. to manage streams:

```python
await nats.jetstream.add_stream(name="INVOICES", subjects=["invoices.>"])
await nats.jetstream.consumers_info("INVOICES")
```
