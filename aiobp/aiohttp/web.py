"""Router that validates method args according to their annotations"""
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Any, TypeVar

from aiohttp import hdrs, web

from .provider import Provider

T = TypeVar("T", bound=Callable[..., Awaitable[Any]])
D = TypeVar("D")


class Router(web.RouteTableDef):
    """Router that validates method args according to their annotations"""

    def __init__(self) -> None:
        super().__init__()
        self.__type_injectors: dict[type[D], Callable[[web.Request], D]] = {
            web.Request: lambda request: request,
        }

    def add_type_injector(self, typ: type[D], factory: Callable[[web.Request], D]) -> None:
        self.__type_injectors[typ] = factory

    def route(self, method: str, path: str, **kwargs: dict[str, dict[str, Any]]) -> Callable[[T], T]:
        """Add route to webserver by decorating request handler"""

        def decorate(handler: T) -> T:
            """Parse handler arguments and return value on startup"""
            # Inspect handler and cache request arguments validation and response encoding.
            provider = Provider(handler, self.__type_injectors)

            @wraps(handler)
            async def wrapped(request: web.Request) -> web.StreamResponse:
                """Process HTTP request by the request handler"""
                try:
                    kwargs = await provider.gather_args(request)
                except TypeError as error:
                    raise web.HTTPBadRequest(body=str(error)) from error

                result = await handler(**kwargs)
                return provider.encode_response(result)

            # Register route for aiohttp server.
            self._items.append(web.RouteDef(method, path, wrapped, kwargs))
            return handler

        return decorate

    def head(self, path: str, **kwargs: dict[str, Any]) -> Callable[[T], T]:
        return self.route(hdrs.METH_HEAD, path, **kwargs)

    def get(self, path: str, **kwargs: dict[str, Any]) -> Callable[[T], T]:
        return self.route(hdrs.METH_GET, path, **kwargs)

    def post(self, path: str, **kwargs: dict[str, Any]) -> Callable[[T], T]:
        return self.route(hdrs.METH_POST, path, **kwargs)

    def put(self, path: str, **kwargs: dict[str, Any]) -> Callable[[T], T]:
        return self.route(hdrs.METH_PUT, path, **kwargs)

    def patch(self, path: str, **kwargs: dict[str, Any]) -> Callable[[T], T]:
        return self.route(hdrs.METH_PATCH, path, **kwargs)

    def delete(self, path: str, **kwargs: dict[str, Any]) -> Callable[[T], T]:
        return self.route(hdrs.METH_DELETE, path, **kwargs)

    def options(self, path: str, **kwargs: dict[str, Any]) -> Callable[[T], T]:
        return self.route(hdrs.METH_OPTIONS, path, **kwargs)

    def view(self, path: str, **kwargs: dict[str, Any]) -> Callable[[T], T]:
        return self.route(hdrs.METH_ANY, path, **kwargs)

# Default router singleton
router = Router()

# Aliases so we can use @web.get("/some/path")
get = router.get
post = router.post
put = router.put
patch = router.patch
delete = router.delete
options = router.options
view = router.view
