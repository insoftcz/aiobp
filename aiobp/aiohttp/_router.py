"""Router that validates method args according to their annotations"""

import inspect
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from functools import wraps
from typing import Any, Optional, TypeVar

from aiohttp import hdrs, web
from typing_extensions import override

from aiobp import log

from ._connection import ClientAddress, ServerHostname, get_client_address, get_server_hostname
from ._http_range import HttpRangeRequest
from ._openapi import OpenAPIBuilder
from ._provider import InjectorFactory, Provider

T = TypeVar("T", bound=Callable[..., Awaitable[Any]])


@dataclass
class _PendingRoute:
    """Route collected by a decorator, registered later by build()."""

    handler: Callable[..., Any]
    method: str
    path: str
    router_type: str  # "api" or "plain"
    content_type: Optional[str] = None
    charset: str = "utf-8"
    tag: Optional[str] = None
    secure: Optional[bool] = None


class ApiRouter:
    """Decorator factory for routes included in OpenAPI/Swagger docs."""

    def __init__(
        self,
        pending: list[_PendingRoute],
        default_content_type: str = "application/json",
        default_charset: str = "utf-8",
        on_result: Optional[Callable[[Any], Any]] = None,
        on_error: Optional[Callable[[BaseException], Any]] = None,
    ) -> None:
        self._pending: list[_PendingRoute] = pending
        self._router_type: str = "api"
        self._default_content_type: str = default_content_type
        self._default_charset: str = default_charset
        self.on_result: Optional[Callable[[Any], Any]] = on_result
        self.on_error: Optional[Callable[[BaseException], Any]] = on_error

    def route(
        self,
        method: str,
        path: str,
        *,
        tag: Optional[str] = None,
        secure: Optional[bool] = None,
        content_type: Optional[str] = None,
    ) -> Callable[[T], T]:
        ct = content_type or self._default_content_type

        def decorate(handler: T) -> T:
            self._pending.append(_PendingRoute(
                handler=handler,
                method=method,
                path=path,
                router_type=self._router_type,
                content_type=ct,
                charset=self._default_charset,
                tag=tag, secure=secure,
            ))
            return handler
        return decorate

    def get(self, path: str, *, content_type: Optional[str] = None, secure: Optional[bool] = None, tag: Optional[str] = None, **kwargs: Any) -> Callable[[T], T]:  # noqa: ANN401, E501
        return self.route(hdrs.METH_GET, path, tag=tag, secure=secure, content_type=content_type, **kwargs)

    def post(self, path: str, *, content_type: Optional[str] = None, secure: Optional[bool] = None, tag: Optional[str] = None, **kwargs: Any) -> Callable[[T], T]:  # noqa: ANN401, E501
        return self.route(hdrs.METH_POST, path, tag=tag, secure=secure, content_type=content_type, **kwargs)

    def put(self, path: str, *, content_type: Optional[str] = None, secure: Optional[bool] = None, tag: Optional[str] = None, **kwargs: Any) -> Callable[[T], T]:  # noqa: ANN401, E501
        return self.route(hdrs.METH_PUT, path, tag=tag, secure=secure, content_type=content_type, **kwargs)

    def patch(self, path: str, *, content_type: Optional[str] = None, secure: Optional[bool] = None, tag: Optional[str] = None, **kwargs: Any) -> Callable[[T], T]:  # noqa: ANN401, E501
        return self.route(hdrs.METH_PATCH, path, tag=tag, secure=secure, content_type=content_type, **kwargs)

    def delete(self, path: str, *, content_type: Optional[str] = None, secure: Optional[bool] = None, tag: Optional[str] = None, **kwargs: Any) -> Callable[[T], T]:  # noqa: ANN401, E501
        return self.route(hdrs.METH_DELETE, path, tag=tag, secure=secure, content_type=content_type, **kwargs)

    def head(self, path: str, *, content_type: Optional[str] = None, secure: Optional[bool] = None, tag: Optional[str] = None, **kwargs: Any) -> Callable[[T], T]:  # noqa: ANN401, E501
        return self.route(hdrs.METH_HEAD, path, tag=tag, secure=secure, content_type=content_type, **kwargs)

    def options(self, path: str, *, content_type: Optional[str] = None, secure: Optional[bool] = None, tag: Optional[str] = None, **kwargs: Any) -> Callable[[T], T]:  # noqa: ANN401, E501
        return self.route(hdrs.METH_OPTIONS, path, tag=tag, secure=secure, content_type=content_type, **kwargs)


class Router(web.RouteTableDef):
    """Coordinates API and plain sub-routers sharing the same pending list."""

    def __init__(
        self,
        default_content_type: str = "text/html",
        default_charset: str = "utf-8",
        on_result: Optional[Callable[[Any], Any]] = None,
        on_error: Optional[Callable[[BaseException], Any]] = None,
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
        self.openapi: OpenAPIBuilder = OpenAPIBuilder()
        self.api: ApiRouter = ApiRouter(self._pending, on_result=on_result, on_error=on_error)

    def add_type_injector(self, typ: type, factory: InjectorFactory) -> None:
        self._type_injectors[typ] = factory

    def mount_docs(self, prefix: str = "") -> None:
        """Serve OpenAPI JSON spec and Swagger UI."""
        url = f"{prefix}/openapi.json"
        spec = self.openapi.build()
        docs = self.openapi.swagger_ui_html(url)

        @self.get(url, content_type="application/json")
        async def openapi_json() -> dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
            return spec

        @self.get(f"{prefix}/docs")
        async def swagger_ui() -> str:  # pyright: ignore[reportUnusedFunction]
            return docs

        log.info("API docs available at %s/docs", prefix)

    @override
    def route(self, method: str, path: str, *, content_type: Optional[str] = None, **kwargs: Any) -> Callable[[T], T]:  # type: ignore[override]
        ct = content_type or self._default_content_type

        def decorate(handler: T) -> T:
            self._pending.append(_PendingRoute(
                handler=handler,
                method=method,
                path=path,
                router_type="plain",
                content_type=ct,
                charset=self._default_charset,
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

    def include(self, instance: object) -> None:
        """Replace unbound class methods in _pending with bound methods from *instance*."""
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
                    tag=p.tag if p.tag is not None else (tag if p.router_type == "api" else None),
                    secure=p.secure,
                ))

    def build(self) -> None:
        """Wrap all pending handlers and populate the aiohttp route table."""
        if self._built:
            return
        self._built = True

        registered: set[tuple[str, str]] = set()
        for entry in self._pending:
            key = (entry.method, entry.path)
            if key in registered:
                msg = f"Duplicate route: {entry.method} {entry.path}"
                raise ValueError(msg)
            registered.add(key)

            self._register(entry)

        self._pending.clear()

    def _register(self, entry: _PendingRoute) -> None:
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
        # Read live off self.api (not snapshotted at decoration time) so that setting
        # router.api.on_result/on_error after routes are decorated still takes effect —
        # this runs once, lazily, at build() time.
        is_api_route = entry.router_type == "api"
        on_result = self.api.on_result if is_api_route else None
        on_error = self.api.on_error if is_api_route else None
        provider = Provider(handler, self._type_injectors)

        @wraps(entry.handler)
        async def wrapped(request: web.Request) -> web.StreamResponse:
            try:
                args = await provider.gather_args(request)
            except TypeError as error:
                raise web.HTTPBadRequest(text=str(error)) from error

            try:
                result: Any = handler(**args)
                if inspect.isawaitable(result):
                    result = await result
            except web.HTTPException:
                raise
            except Exception as error:
                if on_error is None:
                    raise
                return provider.encode_response(
                    on_error(error), content_type=content_type, charset=charset, status=500,
                )

            if on_result is not None:
                result = on_result(result)
            return provider.encode_response(result, content_type=content_type, charset=charset)

        self._items.append(web.RouteDef(entry.method, entry.path, wrapped, {}))
        log.debug("%-4s %-7s %s", entry.router_type, entry.method, entry.path)

        if entry.router_type == "api":
            self.openapi.add_route(
                entry.method, entry.path, entry.handler, self._type_injectors,
                tag=entry.tag, secure=entry.secure,
            )

    @override
    def __iter__(self) -> Iterator[web.AbstractRouteDef]:
        self.build()
        return iter(self._items)


# Default router singleton
router = Router()

# Module-level aliases for plain routes
get = router.get
post = router.post
put = router.put
patch = router.patch
delete = router.delete
options = router.options
