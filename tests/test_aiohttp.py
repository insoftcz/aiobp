
from typing import Annotated, Optional

from aiohttp import web as aioweb
from msgspec import Meta

from aiobp import runner
from aiobp.aiohttp import web
from aiobp.aiohttp.server import WebServer


class RemoteClient:
    def __init__(self, host: str):
        self.host = host

def client_factory(request: aioweb.Request) -> RemoteClient:
    return RemoteClient(request.host)

web.router.add_type_injector(RemoteClient, client_factory)


@web.get("/hello/{who}")
async def hello(
    who: Annotated[str, Meta(description="jojo")],
    client: Annotated[RemoteClient, Meta(description="dependency injection")],
    request: Optional[Annotated[aioweb.Request, Meta(description="dependency injection of request itself")]] = None,
) -> Annotated[str, Meta(description="jojo")]:
    """Test basic usage"""
    print(f"Remote host is: {client.host}")
    print(f"AIOHTTP request: {request}")
    return f"Hello, {who}"


@web.get("/hello")
async def hello2(who: Annotated[str, Meta(description="jojo")]) -> Annotated[str, Meta(description="jojo")]:
    """Test basic usage"""
    return f"Hello, {who}"


@web.get("/maybe/{who}")
async def maybe(who: Optional[Annotated[str, Meta(description="jojo")]] = "Nobody") -> Optional[Annotated[str, Meta(description="jojo")]]:
    return f"Hello, {who}"


@web.get("/maybe")
async def maybe2(who: Optional[Annotated[str, Meta(description="jojo")]] = "Nobody") -> Optional[Annotated[str, Meta(description="jojo")]]:
    return f"Hello, {who}"


async def main():
    server = WebServer(8888)
    await server.start()
    print(await hello("call in code", RemoteClient("not when running in code")))

if __name__ == "__main__":
    runner(main())
