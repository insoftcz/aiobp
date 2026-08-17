"""Router that validates method args according to their annotations"""

from collections.abc import Awaitable, Callable, MutableSequence
from functools import wraps
from typing import Any, Optional, TypeVar

from aiohttp import hdrs, web
from typing_extensions import override

from .openapi import OpenAPIBuilder
from .provider import InjectorFactory, Provider

T = TypeVar("T", bound=Callable[..., Awaitable[Any]])


class _BaseRouter:
    """Shared route-registration logic for REST and HTML routers."""

    def __init__(
        self,
        items: MutableSequence[web.AbstractRouteDef],
        type_injectors: dict[type, InjectorFactory],
        prefix: str = "",
        default_content_type: Optional[str] = None,
    ) -> None:
        self._items: MutableSequence[web.AbstractRouteDef] = items
        self._type_injectors: dict[type, InjectorFactory] = type_injectors
        self._prefix: str = prefix.rstrip("/")
        self._default_content_type: Optional[str] = default_content_type

    def _full_path(self, path: str) -> str:
        return self._prefix + path

    def _wrap(self, handler: T, full_path: str, method: str, content_type: Optional[str], kwargs: dict[str, Any]) -> T:
        """Wrap handler with Provider injection and register the aiohttp route."""
        provider = Provider(handler, self._type_injectors)

        @wraps(handler)
        async def wrapped(request: web.Request) -> web.StreamResponse:
            try:
                args = await provider.gather_args(request)
            except TypeError as error:
                raise web.HTTPBadRequest(text=str(error)) from error
            result: Any = await handler(**args)
            return provider.encode_response(result, content_type=content_type or self._default_content_type)

        self._items.append(web.RouteDef(method, full_path, wrapped, kwargs))
        return handler

    def route(self, method: str, path: str, *, content_type: Optional[str] = None, **kwargs: Any) -> Callable[[T], T]:
        def decorate(handler: T) -> T:
            return self._wrap(handler, self._full_path(path), method, content_type, kwargs)
        return decorate

    def get(self, path: str, *, content_type: Optional[str] = None, **kwargs: Any) -> Callable[[T], T]:
        return self.route(hdrs.METH_GET, path, content_type=content_type, **kwargs)

    def post(self, path: str, *, content_type: Optional[str] = None, **kwargs: Any) -> Callable[[T], T]:
        return self.route(hdrs.METH_POST, path, content_type=content_type, **kwargs)

    def put(self, path: str, *, content_type: Optional[str] = None, **kwargs: Any) -> Callable[[T], T]:
        return self.route(hdrs.METH_PUT, path, content_type=content_type, **kwargs)

    def patch(self, path: str, *, content_type: Optional[str] = None, **kwargs: Any) -> Callable[[T], T]:
        return self.route(hdrs.METH_PATCH, path, content_type=content_type, **kwargs)

    def delete(self, path: str, *, content_type: Optional[str] = None, **kwargs: Any) -> Callable[[T], T]:
        return self.route(hdrs.METH_DELETE, path, content_type=content_type, **kwargs)

    def head(self, path: str, *, content_type: Optional[str] = None, **kwargs: Any) -> Callable[[T], T]:
        return self.route(hdrs.METH_HEAD, path, content_type=content_type, **kwargs)

    def options(self, path: str, *, content_type: Optional[str] = None, **kwargs: Any) -> Callable[[T], T]:
        return self.route(hdrs.METH_OPTIONS, path, content_type=content_type, **kwargs)


class HtmlRouter(_BaseRouter):
    """Routes with Provider injection that return str (rendered as text/html).

    Not included in OpenAPI/Swagger docs.
    """

    def __init__(
        self,
        items: MutableSequence[web.AbstractRouteDef],
        type_injectors: dict[type, InjectorFactory],
        prefix: str = "",
    ) -> None:
        super().__init__(items, type_injectors, prefix, default_content_type="text/html")


class RestRouter(_BaseRouter):
    """Routes with Provider injection that are included in OpenAPI/Swagger docs."""

    def __init__(
        self,
        items: MutableSequence[web.AbstractRouteDef],
        type_injectors: dict[type, InjectorFactory],
        openapi: OpenAPIBuilder,
        prefix: str = "",
    ) -> None:
        super().__init__(items, type_injectors, prefix, default_content_type="application/json")
        self._openapi: OpenAPIBuilder = openapi

    @override
    def route(  # type: ignore[override]
        self,
        method: str,
        path: str,
        *,
        tag: Optional[str] = None,
        secure: Optional[bool] = None,
        content_type: Optional[str] = None,
        **kwargs: Any,
    ) -> Callable[[T], T]:
        def decorate(handler: T) -> T:
            full_path = self._full_path(path)
            _ = self._wrap(handler, full_path, method, content_type, kwargs)
            self._openapi.add_route(method, full_path, handler, self._type_injectors, tag=tag, secure=secure)
            return handler
        return decorate

    @override
    def get(self, path: str, *, tag: Optional[str] = None, secure: Optional[bool] = None, content_type: Optional[str] = None, **kwargs: Any) -> Callable[[T], T]:  # noqa: E501
        return self.route(hdrs.METH_GET, path, tag=tag, secure=secure, content_type=content_type, **kwargs)

    @override
    def post(self, path: str, *, tag: Optional[str] = None, secure: Optional[bool] = None, content_type: Optional[str] = None, **kwargs: Any) -> Callable[[T], T]:  # noqa: E501
        return self.route(hdrs.METH_POST, path, tag=tag, secure=secure, content_type=content_type, **kwargs)

    @override
    def put(self, path: str, *, tag: Optional[str] = None, secure: Optional[bool] = None, content_type: Optional[str] = None, **kwargs: Any) -> Callable[[T], T]:  # noqa: E501
        return self.route(hdrs.METH_PUT, path, tag=tag, secure=secure, content_type=content_type, **kwargs)

    @override
    def patch(self, path: str, *, tag: Optional[str] = None, secure: Optional[bool] = None, content_type: Optional[str] = None, **kwargs: Any) -> Callable[[T], T]:  # noqa: E501
        return self.route(hdrs.METH_PATCH, path, tag=tag, secure=secure, content_type=content_type, **kwargs)

    @override
    def delete(self, path: str, *, tag: Optional[str] = None, secure: Optional[bool] = None, content_type: Optional[str] = None, **kwargs: Any) -> Callable[[T], T]:  # noqa: E501
        return self.route(hdrs.METH_DELETE, path, tag=tag, secure=secure, content_type=content_type, **kwargs)

    @override
    def head(self, path: str, *, tag: Optional[str] = None, secure: Optional[bool] = None, content_type: Optional[str] = None, **kwargs: Any) -> Callable[[T], T]:  # noqa: E501
        return self.route(hdrs.METH_HEAD, path, tag=tag, secure=secure, content_type=content_type, **kwargs)

    @override
    def options(self, path: str, *, tag: Optional[str] = None, secure: Optional[bool] = None, content_type: Optional[str] = None, **kwargs: Any) -> Callable[[T], T]:  # noqa: E501
        return self.route(hdrs.METH_OPTIONS, path, tag=tag, secure=secure, content_type=content_type, **kwargs)


class Router(web.RouteTableDef):
    """Coordinates REST and HTML sub-routers sharing the same route table and injectors."""

    def __init__(
        self,
        title: str = "API",
        version: str = "1.0.0",
        api_prefix: Optional[str] = None,
        html_prefix: str = "",
    ) -> None:
        super().__init__()
        self._type_injectors: dict[type, InjectorFactory] = {
            web.Request: lambda request: request,
        }
        self.openapi: OpenAPIBuilder = OpenAPIBuilder(title=title, version=version)
        self.rest: RestRouter = RestRouter(self._items, self._type_injectors, self.openapi, prefix=api_prefix or "")
        self.html: HtmlRouter = HtmlRouter(self._items, self._type_injectors, prefix=html_prefix)

    def add_type_injector(self, typ: type, factory: InjectorFactory) -> None:
        self._type_injectors[typ] = factory

    # Keep route()/get()/post() etc. on Router itself as a convenience — they
    # delegate to rest so existing @router.get(...) code keeps working.
    @override
    def route(self, method: str, path: str, **kwargs: Any) -> Callable[[T], T]:  # type: ignore[override]
        return self.rest.route(method, path, **kwargs)

    @override
    def get(self, path: str, **kwargs: Any) -> Callable[[T], T]:  # type: ignore[override]
        return self.rest.get(path, **kwargs)

    @override
    def post(self, path: str, **kwargs: Any) -> Callable[[T], T]:  # type: ignore[override]
        return self.rest.post(path, **kwargs)

    @override
    def put(self, path: str, **kwargs: Any) -> Callable[[T], T]:  # type: ignore[override]
        return self.rest.put(path, **kwargs)

    @override
    def patch(self, path: str, **kwargs: Any) -> Callable[[T], T]:  # type: ignore[override]
        return self.rest.patch(path, **kwargs)

    @override
    def delete(self, path: str, **kwargs: Any) -> Callable[[T], T]:  # type: ignore[override]
        return self.rest.delete(path, **kwargs)

    @override
    def head(self, path: str, **kwargs: Any) -> Callable[[T], T]:  # type: ignore[override]
        return self.rest.head(path, **kwargs)

    @override
    def options(self, path: str, **kwargs: Any) -> Callable[[T], T]:  # type: ignore[override]
        return self.rest.options(path, **kwargs)


# Default router singleton
router = Router()

# Aliases so we can use @web.get("/some/path")
get = router.rest.get
post = router.rest.post
put = router.rest.put
patch = router.rest.patch
delete = router.rest.delete
options = router.rest.options
