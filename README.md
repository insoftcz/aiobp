Asyncio Service Boilerplate
===========================

This module provides a foundation for building microservices using Python's `asyncio` library. Key features include:

  * A runner with graceful shutdown
  * A task reference management
  * A flexible configuration provider
  * A logger with colorized output
  * Optional OpenTelemetry logging and distributed tracing
  * Optional [HTTP server with Swagger](https://github.com/insoftcz/aiobp/blob/master/docs/aiohttp.md) built on aiohttp
  * Optional [NATS message bus connector](https://github.com/insoftcz/aiobp/blob/master/docs/nats.md)

No dependencies are enforced by default, so you only install what you need.
For basic usage, no additional Python modules are required.
The table below summarizes which optional dependencies to install based on the features you want to use:

|     aiobp Feature       | Required Module(s) | Extra           |
|-------------------------|--------------------|-----------------|
| config (.conf or .json) | msgspec            | `logging`       |
| config (.yaml)          | msgspec, pyyaml    | `logging_yaml`  |
| OpenTelemetry logging   | opentelemetry-sdk, opentelemetry-exporter-otlp-proto-grpc | `logging_otel`  |
| OpenTelemetry tracing   | opentelemetry-sdk, opentelemetry-exporter-otlp-proto-grpc | `tracing_otel`  |
| [HTTP server + Swagger](https://github.com/insoftcz/aiobp/blob/master/docs/aiohttp.md) | aiohttp, msgspec | `aiohttp` |
| [NATS message bus](https://github.com/insoftcz/aiobp/blob/master/docs/nats.md)          | nats-py, msgspec | `nats`    |

Logs and traces share the same dependency set — install `aiobp[otel]` to get both:

```bash
pip install aiobp[otel]
```

Basic example
-------------

```python
import asyncio

from aiobp import runner

async def main():
    try:
        await asyncio.sleep(60)
    except asyncio.CancelledError:
        print('Saving data...')

runner("Basic example", "0.0.0", main())
```

OpenTelemetry Logging
---------------------

aiobp supports exporting logs to OpenTelemetry collectors (SigNoz, Jaeger, etc.).

### Configuration

Add OTEL settings to your `LoggingConfig`:

```ini
[log]
level = DEBUG
filename = service.log
otel_endpoint = http://localhost:4317
otel_export_interval = 5
```

| Option               | Default | Description                                      |
|----------------------|---------|--------------------------------------------------|
| otel_endpoint        | None    | OTLP gRPC endpoint (e.g. http://localhost:4317)  |
| otel_export_interval | 5       | Export interval in seconds (0 = instant export)  |

### Usage

```python
from dataclasses import dataclass
from aiobp.logging import LoggingConfig, setup_logging, log

@dataclass
class Config:
    log: LoggingConfig = None

# ... load config ...

setup_logging("my-service-name", config.log)
log.info("This message goes to console, file, and OTEL collector")
```

### Resource Attributes

To add custom resource attributes (like location, environment, etc.), set the standard OTEL environment variable before calling `setup_logging`:

```python
import os

os.environ["OTEL_RESOURCE_ATTRIBUTES"] = "location=datacenter1,environment=production"
setup_logging("my-service-name", config.log)
```

### Graceful Fallback

If `otel_endpoint` is configured but OpenTelemetry packages are not installed, a warning is logged and the application continues with console/file logging only.


OpenTelemetry Tracing
---------------------

aiobp also supports exporting distributed traces to OpenTelemetry collectors. Call once at startup, then use `traced()`, `current_span()`, and `start_span()` anywhere in the codebase.

### Setup

```python
from aiobp import __version__
from aiobp.tracing import setup_tracing

setup_tracing("my-service", __version__, config.log.otel_endpoint)
```

Logging and tracing are independent — call either or both. The common pattern is to reuse the same OTLP endpoint:

```python
from aiobp.logging import setup_logging
from aiobp.tracing import setup_tracing

setup_logging("my-service", config.log)                           # logs go to OTel if endpoint set
setup_tracing("my-service", __version__, config.log.otel_endpoint)  # traces share the endpoint
```

### Usage

```python
from aiobp.tracing import traced, current_span

async def do_work():
    result = await call_external_api()
    # Helper doesn't take the span as an argument — reach for the active one:
    current_span().set_attribute("result.id", result.id)
    return result

async with traced("operation.name", {"key": "value"}):
    await do_work()
```

`traced` accepts:

- `attrs` — dict of span attributes.
- `context` — an OTel `Context` for parent propagation.
- `traceparent` — W3C traceparent string (alternative to `context`; the function calls `extract()` for you).
- `suppress` — exception types to log-and-swallow inside the span (defaults to none).
- `errors_only=True` — span is created lazily, only when an exception is raised. Useful for noisy event handlers where you only want to surface failures.

From any nested function call, `current_span()` returns the active span so you can attach attributes without threading the span through arguments.

### Long-lived spans with `start_span`

`traced()` is a context manager — the span ends when the block exits. For spans that need to outlive a single function call (e.g. "caller is waiting for an agent" — open in one event handler, closed in another), use `start_span()` and call `.end()` yourself:

```python
from aiobp.tracing import start_span

# Begin the wait — store the returned span somewhere
wait_span = start_span("queue.wait_for_agent", {"queue.id": 42}, traceparent=caller_traceparent)
self._wait_spans[caller.uuid] = wait_span

# Later, when the wait ends:
span = self._wait_spans.pop(caller.uuid, None)
if span:
    span.end()
```

`start_span` accepts the same `attrs`, `context`, and `traceparent` parameters as `traced`. The returned span is **not** installed as the current context — child spans elsewhere won't auto-nest under it. Use it for pure duration markers.

If tracing isn't configured, `start_span` returns a no-op span; calling `.end()` on it is harmless.

### Propagation

Trace context crosses service boundaries in `traceparent` headers (the
`aiobp.nats` connector does this by itself, see docs/nats.md):

```python
from aiobp.tracing import context_from_headers, propagation_headers, trace_id, use_context

await client.publish(subject, payload, headers=propagation_headers())  # send the current trace

with use_context(context_from_headers(msg.headers)):                   # continue the sender's trace
    log.info("handling %s (trace %s)", msg.subject, trace_id())
```

`new_traceparent()` starts a trace (or wraps a bare trace id) without
OpenTelemetry installed — for services that only relay traces.

### Graceful Fallback

Without an endpoint (or when it is unreachable) `setup_tracing` still installs
a tracer: spans get real trace ids and propagate, they are just not exported.
If `setup_tracing` is never called, or the OpenTelemetry packages aren't
installed, `traced()` becomes a no-op. Application code using `traced()` and
`current_span()` works unchanged whether tracing is on or off.
