"""HTTP server"""

import json
from typing import Optional

from aiohttp import web
from aiohttp.web_routedef import RouteTableDef

from aiobp import log

from .web import Router, router


class WebServer:
    """HTTP server"""

    def __init__(
        self,
        port: int,
        host: str = "127.0.0.1",
        router: Optional[RouteTableDef] = router,
        *,
        docs: bool = True,
    ) -> None:
        self._port: int = port
        self._host: str = host
        self._app: web.Application = web.Application()
        if router:
            _ = self._app.add_routes(router)
        if docs and isinstance(router, Router):
            self._mount_docs(router)

    @property
    def app(self) -> web.Application:
        """Underlying aiohttp Application — use to register middleware, signals, etc."""
        return self._app

    def _mount_docs(self, router: Router) -> None:
        """Serve OpenAPI JSON spec and Swagger UI."""
        spec = router.openapi.build()
        ui_html = router.openapi.swagger_ui_html

        async def openapi_json(_request: web.Request) -> web.Response:
            return web.Response(
                text=json.dumps(spec, indent=2),
                content_type="application/json",
            )

        async def swagger_ui(_request: web.Request) -> web.Response:
            return web.Response(text=ui_html, content_type="text/html")

        _ = self._app.router.add_get("/openapi.json", openapi_json)
        _ = self._app.router.add_get("/docs", swagger_ui)
        log.info("API docs available at http://%s:%s/docs", self._host, self._port)

    async def start(self) -> None:
        """Start webserver"""
        runner = web.AppRunner(self._app)
        await runner.setup()
        site = web.TCPSite(runner, self._host, self._port)
        await site.start()
        log.info("Started http://%s:%s/", self._host, self._port)
