"""Router that validates method args according to their annotations"""

import inspect
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from enum import Enum
from functools import wraps
from typing import Any, Optional, TypeVar

from aiohttp import hdrs, web
from typing_extensions import override

from aiobp import log
from aiobp.aiohttp._connection import ClientAddress, ServerHostname, get_client_address, get_server_hostname
from aiobp.aiohttp._http_range import HttpRangeRequest
from aiobp.aiohttp._openapi import OpenAPIBuilder
from aiobp.aiohttp._provider import InjectorFactory, Provider, ServerError

T = TypeVar("T", bound=Callable[..., Awaitable[Any]])


def _make_openapi_json_handler(spec: dict[str, Any]) -> Callable[[], Awaitable[dict[str, Any]]]:
    """Bind ``spec`` in its own scope — a plain closure over a ``_mount_docs()`` loop variable

    would have every mounted route serve whichever ApiRouter's spec was built last.
    """
    async def openapi_json() -> dict[str, Any]:
        return spec
    return openapi_json


def _make_swagger_ui_handler(html: str) -> Callable[[], Awaitable[str]]:
    """See ``_make_openapi_json_handler`` — same loop-variable-capture reason."""
    async def swagger_ui() -> str:
        return html
    return swagger_ui


def _handle_unexpected_error(  # noqa: PLR0913
    error: Exception,
    request: web.Request,
    *,
    on_error: Optional[Callable[[str, str, BaseException], Any]],
    is_api_route: bool,
    provider: Provider,
    content_type: Optional[str],
    charset: str,
) -> web.StreamResponse:
    """Render an exception that isn't a ``web.HTTPException`` (so didn't already carry its own response).

    ``on_error``, if configured, always wins — it's a deliberate global envelope.
    Otherwise API routes get a structured, logged 500; plain routes re-raise
    unchanged, same as always.
    """
    if on_error is not None:
        return provider.encode_response(
            on_error(request.method, request.path, error), content_type=content_type, charset=charset, status=500,
        )
    if not is_api_route:
        raise error
    log.exception("Unhandled exception in %s %s", request.method, request.path)
    return provider.encode_response(
        ServerError(error).to_response(), content_type=content_type, charset=charset, status=500,
    )


class RouterType(str, Enum):
    """Whether a route is documented in OpenAPI/Swagger or served as a plain, undocumented route."""

    API = "api"
    PLAIN = "plain"

    @override
    def __str__(self) -> str:
        return self.value


@dataclass
class _PendingRoute:
    """Route collected by a decorator, registered later by build()."""

    handler: Callable[..., Any]
    method: str
    path: str
    router_type: RouterType
    content_type: Optional[str] = None
    charset: str = "utf-8"
    tag: Optional[str] = None
    secure: Optional[bool] = None
    responses: Optional[dict[int, type]] = None
    kwargs: dict[str, Any] = field(default_factory=dict)
    api_router: Optional["ApiRouter"] = None


class ApiRouter:
    """Decorator factory for routes included in OpenAPI/Swagger docs.

    Attach it to a ``Router`` by constructing it with that router's own
    pending list — it then shares registration with the router, and gets its
    own ``/docs``/``openapi.json`` mounted under ``prefix``, independent of
    any other ``ApiRouter`` assigned to the same ``Router``. This is how
    you'd run e.g. two separately-documented API versions off one ``Router``,
    typically by subclassing it::

        class MyRouter(Router):
            def __init__(self) -> None:
                super().__init__()
                self.api_v1 = ApiRouter("/api/v1.0", self._pending)
                self.api_v2 = ApiRouter("/api/v2.0", self._pending)

    A path starting with ``/`` is absolute and bypasses ``prefix`` entirely;
    any other path is joined onto it::

        api = ApiRouter("/api/v1.0", pending)

        @api.get("call")           # -> GET /api/v1.0/call
        async def call() -> ...: ...

        @api.get("/scim/Users")    # -> GET /scim/Users (prefix ignored)
        async def scim_users() -> ...: ...
    """

    def __init__(  # noqa: PLR0913, PLR0917
        self,
        prefix: str = "/",
        pending: Optional[list[_PendingRoute]] = None,
        default_content_type: str = "application/json",
        default_charset: str = "utf-8",
        on_result: Optional[Callable[[str, str, Any], Any]] = None,
        on_error: Optional[Callable[[str, str, BaseException], Any]] = None,
    ) -> None:
        if not prefix.startswith("/"):
            msg = f"ApiRouter prefix must start with '/', got {prefix!r}"
            raise ValueError(msg)
        self.prefix: str = prefix
        self._pending: list[_PendingRoute] = pending if pending is not None else []
        self._router_type: RouterType = RouterType.API
        self._default_content_type: str = default_content_type
        self._default_charset: str = default_charset
        self.on_result: Optional[Callable[[str, str, Any], Any]] = on_result
        self.on_error: Optional[Callable[[str, str, BaseException], Any]] = on_error
        self.docs: OpenAPIBuilder = OpenAPIBuilder()

    def _full_path(self, path: str) -> str:
        """Resolve a decorated path against ``prefix``; an absolute path bypasses it."""
        if path.startswith("/"):
            return path
        return f"{self.prefix.rstrip('/')}/{path}"

    def route(  # noqa: PLR0913
        self,
        method: str,
        path: str,
        *,
        tag: Optional[str] = None,
        secure: Optional[bool] = None,
        content_type: Optional[str] = None,
        responses: Optional[dict[int, type]] = None,
        **kwargs: Any,  # noqa: ANN401
    ) -> Callable[[T], T]:
        """Register a route.

        Unrecognized keyword arguments (e.g. ``name=``, ``allow_head=``) are
        forwarded as-is to aiohttp's own route registration, matching
        ``web.RouteTableDef``'s behaviour.
        """
        ct = content_type or self._default_content_type

        def decorate(handler: T) -> T:
            self._pending.append(_PendingRoute(
                handler=handler,
                method=method,
                path=self._full_path(path),
                router_type=self._router_type,
                content_type=ct,
                charset=self._default_charset,
                tag=tag, secure=secure,
                responses=responses,
                kwargs=kwargs,
                api_router=self,
            ))
            return handler
        return decorate

    def get(self, path: str, *, content_type: Optional[str] = None, responses: Optional[dict[int, type]] = None, secure: Optional[bool] = None, tag: Optional[str] = None, **kwargs: Any) -> Callable[[T], T]:  # noqa: ANN401, E501
        return self.route(hdrs.METH_GET, path, tag=tag, secure=secure, content_type=content_type, responses=responses, **kwargs)  # noqa: E501

    def post(self, path: str, *, content_type: Optional[str] = None, responses: Optional[dict[int, type]] = None, secure: Optional[bool] = None, tag: Optional[str] = None, **kwargs: Any) -> Callable[[T], T]:  # noqa: ANN401, E501
        return self.route(hdrs.METH_POST, path, tag=tag, secure=secure, content_type=content_type, responses=responses, **kwargs)  # noqa: E501

    def put(self, path: str, *, content_type: Optional[str] = None, responses: Optional[dict[int, type]] = None, secure: Optional[bool] = None, tag: Optional[str] = None, **kwargs: Any) -> Callable[[T], T]:  # noqa: ANN401, E501
        return self.route(hdrs.METH_PUT, path, tag=tag, secure=secure, content_type=content_type, responses=responses, **kwargs)  # noqa: E501

    def patch(self, path: str, *, content_type: Optional[str] = None, responses: Optional[dict[int, type]] = None, secure: Optional[bool] = None, tag: Optional[str] = None, **kwargs: Any) -> Callable[[T], T]:  # noqa: ANN401, E501
        return self.route(hdrs.METH_PATCH, path, tag=tag, secure=secure, content_type=content_type, responses=responses, **kwargs)  # noqa: E501

    def delete(self, path: str, *, content_type: Optional[str] = None, responses: Optional[dict[int, type]] = None, secure: Optional[bool] = None, tag: Optional[str] = None, **kwargs: Any) -> Callable[[T], T]:  # noqa: ANN401, E501
        return self.route(hdrs.METH_DELETE, path, tag=tag, secure=secure, content_type=content_type, responses=responses, **kwargs)  # noqa: E501

    def head(self, path: str, *, content_type: Optional[str] = None, responses: Optional[dict[int, type]] = None, secure: Optional[bool] = None, tag: Optional[str] = None, **kwargs: Any) -> Callable[[T], T]:  # noqa: ANN401, E501
        return self.route(hdrs.METH_HEAD, path, tag=tag, secure=secure, content_type=content_type, responses=responses, **kwargs)  # noqa: E501

    def options(self, path: str, *, content_type: Optional[str] = None, responses: Optional[dict[int, type]] = None, secure: Optional[bool] = None, tag: Optional[str] = None, **kwargs: Any) -> Callable[[T], T]:  # noqa: ANN401, E501
        return self.route(hdrs.METH_OPTIONS, path, tag=tag, secure=secure, content_type=content_type, responses=responses, **kwargs)  # noqa: E501


class Router(web.RouteTableDef):
    """Coordinates any number of ``ApiRouter``s and plain routes sharing one pending list.

    Comes with no ``ApiRouter`` of its own. Attach one (or several) by
    constructing it with this router's own ``_pending`` list, typically in a
    subclass — see ``ApiRouter``'s docstring. Use ``BuiltinRouter`` instead
    for the common case of a single default ``api`` ApiRouter created for you.
    """

    def __init__(
        self,
        default_content_type: str = "text/html",
        default_charset: str = "utf-8",
    ) -> None:
        super().__init__()
        self._type_injectors: dict[type, InjectorFactory] = {
            web.Request: lambda request: request,
            HttpRangeRequest: HttpRangeRequest,
            ServerHostname: get_server_hostname,
            ClientAddress: get_client_address,
        }
        self._pending: list[_PendingRoute] = []
        self._built: bool = False
        self._default_content_type: str = default_content_type
        self._default_charset: str = default_charset

    def add_type_injector(self, typ: type, factory: InjectorFactory) -> None:
        self._type_injectors[typ] = factory

    def _api_routers(self) -> list[ApiRouter]:
        """Every ``ApiRouter`` attached to this router, e.g. ``self.api``/``self.api_v2``."""
        seen: list[ApiRouter] = []
        for value in vars(self).values():
            if isinstance(value, ApiRouter) and value not in seen:
                seen.append(value)
        return seen

    def _mount_docs(self) -> None:
        """Serve each ApiRouter's OpenAPI JSON spec and Swagger UI, under its own prefix."""
        for api_router in self._api_routers():
            docs = api_router.docs
            base = api_router.prefix.rstrip("/")

            url = f"{base}/openapi.json"
            spec = docs.build()
            html = docs.swagger_ui_html(url)

            self.get(url, content_type="application/json")(_make_openapi_json_handler(spec))
            self.get(f"{base}/docs")(_make_swagger_ui_handler(html))

            log.info("API docs available at %s/docs", base)

    @override
    def route(self, method: str, path: str, *, content_type: Optional[str] = None, **kwargs: Any) -> Callable[[T], T]:  # type: ignore[override]
        """Register a route.

        Unrecognized keyword arguments (e.g. ``name=``, ``allow_head=``) are
        forwarded as-is to aiohttp's own route registration, matching
        ``web.RouteTableDef``'s behaviour.
        """
        ct = content_type or self._default_content_type

        def decorate(handler: T) -> T:
            self._pending.append(_PendingRoute(
                handler=handler,
                method=method,
                path=path,
                router_type=RouterType.PLAIN,
                content_type=ct,
                charset=self._default_charset,
                kwargs=kwargs,
            ))
            return handler
        return decorate

    @override
    def get(self, path: str, *, content_type: Optional[str] = None, **kwargs: Any) -> Callable[[T], T]:  # type: ignore[override]
        return self.route(hdrs.METH_GET, path, content_type=content_type, **kwargs)

    @override
    def post(self, path: str, *, content_type: Optional[str] = None, **kwargs: Any) -> Callable[[T], T]:  # type: ignore[override]
        return self.route(hdrs.METH_POST, path, content_type=content_type, **kwargs)

    @override
    def put(self, path: str, *, content_type: Optional[str] = None, **kwargs: Any) -> Callable[[T], T]:  # type: ignore[override]
        return self.route(hdrs.METH_PUT, path, content_type=content_type, **kwargs)

    @override
    def patch(self, path: str, *, content_type: Optional[str] = None, **kwargs: Any) -> Callable[[T], T]:  # type: ignore[override]
        return self.route(hdrs.METH_PATCH, path, content_type=content_type, **kwargs)

    @override
    def delete(self, path: str, *, content_type: Optional[str] = None, **kwargs: Any) -> Callable[[T], T]:  # type: ignore[override]
        return self.route(hdrs.METH_DELETE, path, content_type=content_type, **kwargs)

    @override
    def head(self, path: str, *, content_type: Optional[str] = None, **kwargs: Any) -> Callable[[T], T]:  # type: ignore[override]
        return self.route(hdrs.METH_HEAD, path, content_type=content_type, **kwargs)

    @override
    def options(self, path: str, *, content_type: Optional[str] = None, **kwargs: Any) -> Callable[[T], T]:  # type: ignore[override]
        return self.route(hdrs.METH_OPTIONS, path, content_type=content_type, **kwargs)

    def include(self, instance: object) -> None:  # noqa: C901
        """Replace unbound class methods in _pending with bound methods from *instance*.

        Also accepts a module. Route-decorated free functions need no binding
        (they aren't methods), so this instead assigns fallback OpenAPI metadata
        to any of its API routes:

        - ``tag`` — the module's ``__tag__`` if it defines one, else the last
          segment of its ``__name__`` — applied to routes that don't already
          have one.
        - ``responses`` — the module's ``__responses__`` dict, if it defines
          one — merged into each route's own ``responses`` (the route's own
          entries win on a status-code conflict).

        This also turns "import a routes module purely to run its decorators"
        into a real use of that import, so static analysis stops flagging it
        as unused::

            from myapp.api import users
            router.include(users)
        """
        if inspect.ismodule(instance):
            tag = getattr(instance, "__tag__", None) or instance.__name__.rsplit(".", 1)[-1]
            responses = getattr(instance, "__responses__", None)
            for p in self._pending:
                if p.router_type != RouterType.API or getattr(p.handler, "__module__", None) != instance.__name__:
                    continue
                if p.tag is None:
                    p.tag = tag
                if responses:
                    p.responses = {**responses, **(p.responses or {})}
            return

        tag = type(instance).__name__
        seen: set[int] = set()
        for attr_name in dir(instance):
            bound = getattr(instance, attr_name, None)
            if bound is None or not callable(bound):
                continue
            func = getattr(bound, "__func__", None)
            if func is None or id(func) in seen:
                continue
            matching = [p for p in self._pending if p.handler is func]
            if not matching:
                continue
            seen.add(id(func))
            self._pending[:] = [p for p in self._pending if p.handler is not func]
            for p in matching:
                self._pending.append(_PendingRoute(
                    handler=bound,
                    method=p.method,
                    path=p.path,
                    router_type=p.router_type,
                    content_type=p.content_type,
                    charset=p.charset,
                    tag=p.tag if p.tag is not None else (tag if p.router_type == RouterType.API else None),
                    secure=p.secure,
                    responses=p.responses,
                    kwargs=p.kwargs,
                    api_router=p.api_router,
                ))

    def build(self) -> None:
        """Wrap all pending handlers and populate the aiohttp route table."""
        if self._built:
            return
        self._built = True

        self._process_pending()
        # docs needs to have router table populated
        self._mount_docs()
        # but they add routes so we have to process them
        self._process_pending()

    def _process_pending(self) -> None:
        """Register pending decorators"""
        registered: set[tuple[str, str]] = set()
        for entry in self._pending:
            key = (entry.method, entry.path)
            if key in registered:
                msg = f"Duplicate route: {entry.method} {entry.path}"
                raise ValueError(msg)
            registered.add(key)

            self._register(entry)

        self._pending.clear()

    def _register(self, entry: _PendingRoute) -> None:  # noqa: C901
        """Create a Provider-wrapped handler and add it to the route table."""
        handler = entry.handler
        params = list(inspect.signature(handler).parameters.values())
        if params and params[0].name == "self" and params[0].annotation is inspect.Parameter.empty:
            msg = (
                f'Handler "{handler.__qualname__}" has an unbound "self" parameter.'
                f" Call router.include(instance) to bind it."
            )
            raise TypeError(msg)
        content_type = entry.content_type
        charset = entry.charset
        # Read live off entry.api_router (not snapshotted at decoration time) so that
        # setting its on_result/on_error after routes are decorated still takes effect —
        # this runs once, lazily, at build() time.
        is_api_route = entry.api_router is not None
        on_result = entry.api_router.on_result if entry.api_router is not None else None
        on_error = entry.api_router.on_error if entry.api_router is not None else None
        provider = Provider(handler, self._type_injectors)

        @wraps(entry.handler)
        async def wrapped(request: web.Request) -> web.StreamResponse:
            try:
                args = await provider.gather_args(request)
            except web.HTTPException as error:
                # RequestValidationError (an ApiError, hence a web.HTTPException) already
                # carries its own status/JSON body — just let it propagate, except plain
                # routes keep their old plain-text 400 instead of a JSON error body.
                if not is_api_route:
                    raise web.HTTPBadRequest(text=str(error)) from error
                raise
            except Exception as error:  # noqa: BLE001 - _handle_unexpected_error re-raises for plain routes
                return _handle_unexpected_error(
                    error, request, on_error=on_error, is_api_route=is_api_route,
                    provider=provider, content_type=content_type, charset=charset,
                )

            try:
                result: Any = handler(**args)
                if inspect.isawaitable(result):
                    result = await result
            except web.HTTPException:
                # Covers both aiohttp's own web.HTTPGone()-style exceptions and our own
                # ApiError subclasses — both already are the response, so just propagate.
                raise
            except Exception as error:  # noqa: BLE001 - _handle_unexpected_error re-raises for plain routes
                return _handle_unexpected_error(
                    error, request, on_error=on_error, is_api_route=is_api_route,
                    provider=provider, content_type=content_type, charset=charset,
                )

            if on_result is not None:
                result = on_result(request.method, request.path, result)
            return provider.encode_response(result, content_type=content_type, charset=charset)

        self._items.append(web.RouteDef(entry.method, entry.path, wrapped, entry.kwargs))
        log.debug("%-5s %-7s %s", entry.router_type, entry.method, entry.path)

        if entry.api_router is not None:
            entry.api_router.docs.add_route(
                entry.method, entry.path, entry.handler, self._type_injectors,
                tag=entry.tag, secure=entry.secure, content_type=entry.content_type,
                responses=entry.responses,
            )

    @override
    def __iter__(self) -> Iterator[web.AbstractRouteDef]:
        self.build()
        return iter(self._items)


class BuiltinRouter(Router):
    """A ``Router`` with a default ``api`` ``ApiRouter`` already attached.

    This is what the package's default ``router`` singleton uses — the
    common case of a single documented API surface plus plain routes.
    Additional ``ApiRouter``s (e.g. a versioned ``api_v2``) can still be
    attached the same way as on a bare ``Router``.
    """

    def __init__(
        self,
        default_content_type: str = "text/html",
        default_charset: str = "utf-8",
        on_result: Optional[Callable[[str, str, Any], Any]] = None,
        on_error: Optional[Callable[[str, str, BaseException], Any]] = None,
    ) -> None:
        super().__init__(default_content_type=default_content_type, default_charset=default_charset)
        self.api: ApiRouter = ApiRouter(pending=self._pending, on_result=on_result, on_error=on_error)


# Default router singleton
router = BuiltinRouter()

# Module-level aliases for plain routes
get = router.get
post = router.post
put = router.put
patch = router.patch
delete = router.delete
options = router.options
