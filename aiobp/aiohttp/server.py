"""HTTP server"""

from typing import Optional

from aiohttp import web
from aiohttp.web_routedef import RouteTableDef

from aiobp import log

from .web import router


class WebServer:
    """HTTP server"""

    def __init__(self, port: int, host: str = "127.0.0.1", router: Optional[RouteTableDef] = router):
        self.__port = port
        self.__host = host
        self.__app = web.Application()
        if router:
            self.__app.add_routes(router)

    async def start(self) -> None:
        """Start webserver"""
        runner = web.AppRunner(self.__app)
        await runner.setup()
        site = web.TCPSite(runner, self.__host, self.__port)
        await site.start()
        log.info("Started http://%s:%s/", self.__host, self.__port)
