Asyncio Service Boilerplate
===========================

This module provides a foundation for building microservices using Python's `asyncio` library. Key features include:

  * A runner with graceful shutdown
  * A task reference management
  * A flexible configuration provider
  * A logger with colorized output
  * Optional OpenTelemetry logging and distributed tracing

No dependencies are enforced by default, so you only install what you need.
For basic usage, no additional Python modules are required.
The table below summarizes which optional dependencies to install based on the features you want to use:

|     aiobp Feature       | Required Module(s) | Extra           |
|-------------------------|--------------------|-----------------|
| config (.conf or .json) | msgspec            | `logging`       |
| config (.yaml)          | msgspec, pyyaml    | `logging_yaml`  |
| OpenTelemetry logging   | opentelemetry-sdk, opentelemetry-exporter-otlp-proto-grpc | `logging_otel`  |
| OpenTelemetry tracing   | opentelemetry-sdk, opentelemetry-exporter-otlp-proto-grpc | `tracing_otel`  |
| HTTP server + Swagger   | aiohttp, msgspec                                          | `aiohttp`       |

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

runner(main())
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

### Graceful Fallback

If `setup_tracing` is never called, or the OpenTelemetry packages aren't installed, `traced()` becomes a no-op. Application code using `traced()` and `current_span()` works unchanged whether tracing is on or off.


## HTTP Server (aiohttp helper)

`aiobp.aiohttp` provides a thin layer on top of aiohttp that gives you:

- **Automatic argument injection** from path params, query params, or custom factories
- **Documented parameters** via `Annotated[type, Meta(description=...)]` — enforced at registration time
- **Auto-generated Swagger UI** at `/docs` and OpenAPI JSON at `/openapi.json`
- **Two routing styles** — `router.api` (documented in Swagger, JSON default) and `router.get()` / `router.post()` etc. (plain undocumented routes, text/html default)
- **Dependency injection** for any type via `add_type_injector`

Install the optional extra:

```bash
pip install aiobp[aiohttp]
```

### Quick start

```python
from typing import Annotated
from msgspec import Meta
from aiobp import runner
from aiobp.aiohttp import Router, WebServer

router = Router(title="My API", version="1.0.0")

@router.api.get("/hello/{who}", tag="Greetings")
async def hello(who: Annotated[str, Meta(description="Name to greet")]) -> Annotated[str, Meta(description="Greeting")]:
    """Say hello by name"""
    return f"Hello, {who}"

async def main():
    server = WebServer(8888, router=router)
    await server.start()

runner(main())
```

Open `http://localhost:8888/docs` for the interactive Swagger UI.

### Argument resolution

Parameters are resolved automatically in this order: **path** → **query string**.
Every user-facing parameter must be annotated with `Annotated[type, Meta(description=...)]` — this enforces documentation and provides metadata for the OpenAPI spec.

```python
@router.api.get("/greet")
async def greet(
    who: Annotated[str, Meta(description="Name to greet")],
    age: Optional[Annotated[int, Meta(description="Age")]] = None,  # optional query param
) -> Annotated[str, Meta(description="Greeting")]:
    return f"Hello {who}, age {age}" if age else f"Hello {who}"
```

### Argument source

By default, parameters are resolved by trying the URL path first, then the query string.
You can restrict or change the source with the `Path`, `Query`, and `Body` markers inside `Annotated`:

```python
from aiobp.aiohttp import Path, Query, Body
```

| Marker | Resolves from | Typical use |
|--------|--------------|-------------|
| `Path` | URL path parameters only | `/users/{user_id}` |
| `Query` | Query string only | `?page=2&limit=10` |
| `Body` | Request body | JSON, form data, file upload |
| *(none)* | Path first, then query *(default)* | Simple handlers |

```python
@router.api.get("/users/{user_id}")
async def get_user(
    user_id: Annotated[int, Meta(description="User ID"), Path],
    fields: Annotated[str, Meta(description="Comma-separated fields"), Query],
) -> Annotated[str, Meta(description="User")]:
    ...
```

#### `Body` source

`Body` resolves the argument from the request body. The parsing strategy is chosen automatically based on the argument type and the request's `Content-Type`:

| Argument type | Content-Type | Behaviour |
|---|---|---|
| `msgspec.Struct` | `application/json` | Decode JSON into the struct |
| `msgspec.Struct` | `application/x-www-form-urlencoded` or `multipart/form-data` | Convert form fields into the struct |
| `bytes` | `multipart/form-data` | Extract the file field matching the parameter name |
| `bytes` | anything else | Read the raw request body |
| simple type (`str`, `int`, …) | `application/json` | Extract a single field by parameter name from JSON |
| simple type | form / multipart | Extract a single field by parameter name from form data |

```python
import msgspec

class CreateItem(msgspec.Struct):
    name: str
    price: float

# JSON body → struct
@router.api.post("/items", tag="Items")
async def create_item(
    item: Annotated[CreateItem, Meta(description="Item to create"), Body],
) -> Annotated[str, Meta(description="Item ID")]:
    return f"created {item.name}"

# File upload
@router.api.post("/upload", tag="Files")
async def upload(
    file: Annotated[bytes, Meta(description="File to upload"), Body],
) -> Annotated[str, Meta(description="Size")]:
    return str(len(file))
```

> **Note:** Only one `Body` argument per handler is allowed. Declaring two raises `TypeError` at registration time.

### API vs plain routes

| Style | Appears in Swagger | Return type | Default content-type |
|---|---|---|---|
| `router.api` | yes | `str` / `bytes` / `web.StreamResponse` | `application/json` |
| `router.get()` / `router.post()` etc. | no | `str` / `bytes` / `web.StreamResponse` | `text/html` |

```python
# API — documented in Swagger
@router.api.get("/api/users", tag="Users")
async def list_users() -> Annotated[str, Meta(description="User list")]:
    return "[]"

# Plain route — not in Swagger, returns text/html
@router.get("/dashboard")
async def dashboard() -> str:
    return "<h1>Dashboard</h1>"
```

Both styles support a `content_type` parameter on the decorator to override the response content type — useful for serving binary data:

```python
@router.get("/audio/{id}", content_type="audio/mpeg")
async def stream_audio(id: Annotated[str, Meta(description="Track ID")]) -> bytes:
    return open(f"{id}.mp3", "rb").read()
```

### Class-based routing with `include()`

For larger applications you can group related endpoints in a class using the same `router.api` and `router.get()` / `router.post()` decorators, then mount them with `Router.include()`:

```python
router = Router()

class ItemRoutes:
    @router.api.get("/items/{item_id}", tag="Items")
    async def get_item(
        self, item_id: Annotated[int, Meta(description="Item ID"), Path],
    ) -> Annotated[str, Meta(description="Item")]:
        return f"Item {item_id}"

    @router.api.post("/items", tag="Items")
    async def create_item(
        self, item: Annotated[CreateItem, Meta(description="New item"), Body],
    ) -> Annotated[str, Meta(description="Item ID")]:
        return f"created {item.name}"

    @router.get("/items/page")
    async def item_page(self) -> str:
        return "<h1>Items</h1>"

router.include(ItemRoutes())
```

> **Note:** The `Router` must be created **before** the class definition so the decorators can reference it.

`include()` automatically sets the OpenAPI tag to the class name (e.g. `ItemRoutes`) for all API routes that don't already specify a `tag=...` in the decorator.

This registers:

| Method | Path | Style |
|--------|------|-------|
| GET | `/items/{item_id}` | api |
| POST | `/items` | api |
| GET | `/items/page` | plain |

Decorator paths are always the full path — there is no prefix manipulation.

Duplicate routes (same HTTP method + path) raise `ValueError` at registration time, whether they come from decorators or `include()`.

### Dependency injection

Register a factory for any type with `add_type_injector`. The factory receives the raw `aiohttp.web.Request` and returns an instance. Any handler that declares that type as a parameter gets it injected automatically — no `Annotated`/`Meta` required.

```python
from dataclasses import dataclass
from aiohttp import web

@dataclass
class User:
    username: str
    email: str

def user_from_request(request: web.Request) -> User:
    token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    user = token_store.get(token)
    if user is None:
        raise web.HTTPUnauthorized(text="Invalid token")
    return user

router.add_type_injector(User, user_from_request)

# Now any handler can declare `user: User` and it is resolved automatically:
@router.api.get("/me", tag="Auth")
async def me(user: User) -> Annotated[str, Meta(description="Current user")]:
    return f"{user.username} <{user.email}>"
```

The built-in `web.Request` injector is always registered — declare `request: web.Request` in any handler to receive the raw request.

### Authentication & Swagger

Call `router.openapi.add_bearer_auth()` to add a Bearer token scheme to the Swagger UI. Mark individual endpoints with `secure=False` to make them publicly accessible:

```python
router.openapi.add_bearer_auth()  # all endpoints require auth by default

@router.api.post("/auth/token", tag="Auth", secure=False)  # public
async def obtain_token(
    username: Annotated[str, Meta(description="Username")],
    password: Annotated[str, Meta(description="Password")],
) -> Annotated[str, Meta(description="Bearer token")]:
    ...

@router.api.get("/me", tag="Auth")  # protected (inherits global auth)
async def me(user: User) -> Annotated[str, Meta(description="User info")]:
    ...
```

For OAuth2 with a token endpoint:

```python
router.openapi.add_oauth2("/auth/token", scopes={"read": "Read access", "write": "Write access"})
```

### WebServer options

```python
WebServer(
    port=8888,
    host="127.0.0.1",  # default
    router=router,     # your Router instance
    docs=True,         # serve /docs and /openapi.json (default)
)
```

Access the underlying `aiohttp.web.Application` for middleware or extra routes:

```python
server = WebServer(8888, router=router)
server.app.middlewares.append(my_middleware)
await server.start()
```

## More complex example

A complete service with an API, plain routes, Bearer token authentication,
and dependency injection:

```python
import asyncio
import secrets
from dataclasses import dataclass
from typing import Annotated, Optional

from aiohttp import web
from msgspec import Meta

from aiobp import runner
from aiobp.aiohttp import Router, WebServer

router = Router(title="My Service", version="1.0.0")
router.openapi.add_bearer_auth()  # protect all REST endpoints by default


# --- Auth model & token store ---

@dataclass
class User:
    username: str
    email: str

_CREDENTIALS = {"kenny": "password123"}
_USERS = {"kenny": User("kenny", "kenny@example.com")}
_TOKENS: dict[str, User] = {}


def _user_from_request(request: web.Request) -> User:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise web.HTTPUnauthorized(text="Missing Authorization header")
    user = _TOKENS.get(auth.removeprefix("Bearer ").strip())
    if user is None:
        raise web.HTTPUnauthorized(text="Invalid or expired token")
    return user

router.add_type_injector(User, _user_from_request)


# --- API endpoints (appear in Swagger) ---

@router.api.post("/auth/token", tag="Auth", secure=False)
async def obtain_token(
    username: Annotated[str, Meta(description="Username")],
    password: Annotated[str, Meta(description="Password")],
) -> Annotated[str, Meta(description="Bearer token")]:
    """Obtain a bearer token"""
    expected = _CREDENTIALS.get(username)
    if expected is None or not secrets.compare_digest(expected, password):
        raise web.HTTPUnauthorized(text="Invalid credentials")
    token = secrets.token_urlsafe(32)
    _TOKENS[token] = _USERS[username]
    return token


@router.api.get("/me", tag="Auth")
async def me(user: User) -> Annotated[str, Meta(description="Current user")]:
    """Return the authenticated user's info"""
    return f"{user.username} <{user.email}>"


@router.api.get("/greet", tag="Greet")
async def greet(
    who: Annotated[str, Meta(description="Name to greet")],
    age: Optional[Annotated[int, Meta(description="Age")]] = None,
) -> Annotated[str, Meta(description="Greeting")]:
    """Greet with optional age"""
    return f"Hello {who}, age {age}" if age else f"Hello {who}"


# --- Plain routes (not in Swagger, return str → text/html) ---

@router.get("/")
async def index() -> str:
    return "<h1>My Service</h1><a href='/docs'>API docs</a>"


@router.get("/health")
async def health(request: web.Request) -> str:
    return f"ok@{request.host}"


@router.get("/profile/{name}")
async def profile(name: Annotated[str, Meta(description="Username")]) -> str:
    return f"<h1>Profile: {name}</h1>"


# --- Start ---

async def main() -> None:
    server = WebServer(8888, router=router)
    await server.start()
    await asyncio.Event().wait()  # run until Ctrl+C


if __name__ == "__main__":
    runner(main())
```

Get a token and call a protected endpoint:

```bash
# Obtain token
curl -X POST "http://localhost:8888/auth/token?username=kenny&password=password123"
# → eyJ...

# Call protected endpoint
curl -H "Authorization: Bearer eyJ..." http://localhost:8888/me
# → kenny <kenny@example.com>
```
