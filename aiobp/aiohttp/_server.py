"""HTTP server"""

from collections.abc import Awaitable
from typing import Any, Callable, Optional

from aiohttp import web
from aiohttp.web_routedef import RouteTableDef

from aiobp import log, on_shutdown

from ._router import router

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]
Middleware = Callable[[web.Request, Handler], Awaitable[web.StreamResponse]]


class WebServer:
    """HTTP server"""

    def __init__(
        self,
        port: int,
        host: str = "127.0.0.1",
        router: Optional[RouteTableDef] = router,
        **kwargs: Any,
    ) -> None:
        self._port: int = port
        self._host: str = host
        self._app: web.Application = web.Application(**kwargs)
        if router is not None:
            _ = self._app.add_routes(router)

    @property
    def app(self) -> web.Application:
        """Underlying aiohttp Application — use to register middleware, signals, etc."""
        return self._app

    async def start(self, **kwargs: Any) -> None:
        """Start webserver"""
        runner = web.AppRunner(self._app, **kwargs)
        on_shutdown(runner.shutdown)
        on_shutdown(runner.cleanup, after_tasks_cancel=True)
        await runner.setup()
        site = web.TCPSite(runner, self._host, self._port)
        await site.start()
        log.info("Started http://%s:%s/", self._host, self._port)
        on_shutdown(site.stop)
