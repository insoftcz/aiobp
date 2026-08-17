"""Unit tests for aiobp.aiohttp"""

import unittest
from typing import Annotated, Optional
from unittest.mock import MagicMock

from aiohttp import web as aioweb
from aiohttp.test_utils import AioHTTPTestCase, unittest_run_loop
from msgspec import Meta

from aiobp.aiohttp.provider import Provider
from aiobp.aiohttp.web import Router

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_request(*, match_info: dict = {}, query: dict = {}) -> aioweb.Request:
    """Build a minimal mock aiohttp Request."""
    request = MagicMock(spec=aioweb.Request)
    request.match_info = match_info
    request.query = query
    return request


# ---------------------------------------------------------------------------
# Provider unit tests (no HTTP server needed)
# ---------------------------------------------------------------------------

class TestProviderGetAnnotation(unittest.TestCase):

    def test_plain_annotated(self) -> None:
        hint = Annotated[str, Meta(description="x")]
        typ, optional, meta = Provider.get_annotation(hint)
        self.assertIs(typ, str)
        self.assertFalse(optional)
        self.assertIsInstance(meta, Meta)

    def test_optional_annotated(self) -> None:
        hint = Optional[Annotated[str, Meta(description="x")]]
        typ, optional, meta = Provider.get_annotation(hint)
        self.assertIs(typ, str)
        self.assertTrue(optional)

    def test_plain_type_raises(self) -> None:
        with self.assertRaises(TypeError):
            Provider.get_annotation(str)

    def test_annotated_without_meta_raises(self) -> None:
        with self.assertRaises(TypeError):
            Provider.get_annotation(Annotated[str, "not a Meta"])


class TestProviderGatherArgs(unittest.IsolatedAsyncioTestCase):

    def _make_provider(self, handler, injectors=None) -> Provider:
        return Provider(handler, injectors or {aioweb.Request: lambda request: request})

    async def test_path_argument(self) -> None:
        async def handler(who: Annotated[str, Meta(description="name")]) -> str: ...
        provider = self._make_provider(handler)
        request = make_request(match_info={"who": "world"})
        args = await provider.gather_args(request)
        self.assertEqual(args, {"who": "world"})

    async def test_query_argument(self) -> None:
        async def handler(who: Annotated[str, Meta(description="name")]) -> str: ...
        provider = self._make_provider(handler)
        request = make_request(query={"who": "world"})
        args = await provider.gather_args(request)
        self.assertEqual(args, {"who": "world"})

    async def test_missing_required_raises(self) -> None:
        async def handler(who: Annotated[str, Meta(description="name")]) -> str: ...
        provider = self._make_provider(handler)
        request = make_request()
        with self.assertRaises(TypeError):
            await provider.gather_args(request)

    async def test_optional_defaults_to_none(self) -> None:
        async def handler(who: Optional[Annotated[str, Meta(description="name")]] = None) -> Optional[str]: ...
        provider = self._make_provider(handler)
        request = make_request()
        args = await provider.gather_args(request)
        self.assertIsNone(args["who"])

    async def test_optional_with_default(self) -> None:
        async def handler(who: Optional[Annotated[str, Meta(description="name")]] = "Nobody") -> Optional[str]: ...
        provider = self._make_provider(handler)
        request = make_request()
        args = await provider.gather_args(request)
        self.assertEqual(args["who"], "Nobody")

    async def test_type_coercion_to_int(self) -> None:
        async def handler(count: Annotated[int, Meta(description="n")]) -> int: ...
        provider = self._make_provider(handler)
        request = make_request(query={"count": "42"})
        args = await provider.gather_args(request)
        self.assertEqual(args["count"], 42)
        self.assertIsInstance(args["count"], int)

    async def test_injected_type(self) -> None:
        class Service:
            pass

        instance = Service()
        injectors = {
            aioweb.Request: lambda request: request,
            Service: lambda request: instance,
        }

        async def handler(svc: Service) -> None: ...
        provider = Provider(handler, injectors)
        request = make_request()
        args = await provider.gather_args(request)
        self.assertIs(args["svc"], instance)

    async def test_injected_request(self) -> None:
        async def handler(request: aioweb.Request) -> None: ...
        provider = self._make_provider(handler)
        mock_request = make_request()
        args = await provider.gather_args(mock_request)
        self.assertIs(args["request"], mock_request)


# ---------------------------------------------------------------------------
# Router integration tests (real aiohttp test server)
# ---------------------------------------------------------------------------

def build_app() -> aioweb.Application:
    """Create a fresh app with its own router for each test."""
    router = Router(api_prefix="")  # no prefix for tests

    @router.rest.get("/hello/{who}")
    async def hello(who: Annotated[str, Meta(description="name")]) -> str:
        return f"Hello, {who}"

    @router.rest.get("/greet")
    async def greet(who: Annotated[str, Meta(description="name")]) -> str:
        return f"Hello, {who}"

    @router.rest.get("/maybe")
    async def maybe(who: Optional[Annotated[str, Meta(description="name")]] = "Nobody") -> str:
        return f"Hello, {who or 'Nobody'}"

    app = aioweb.Application()
    app.add_routes(router)
    return app


class TestRouter(AioHTTPTestCase):

    async def get_application(self) -> aioweb.Application:
        return build_app()

    @unittest_run_loop
    async def test_path_param(self) -> None:
        resp = await self.client.get("/hello/world")
        self.assertEqual(resp.status, 200)
        self.assertEqual(await resp.text(), "Hello, world")
        self.assertEqual(resp.content_type, "application/json")

    @unittest_run_loop
    async def test_query_param(self) -> None:
        resp = await self.client.get("/greet?who=world")
        self.assertEqual(resp.status, 200)
        self.assertEqual(await resp.text(), "Hello, world")

    @unittest_run_loop
    async def test_missing_required_param_returns_400(self) -> None:
        resp = await self.client.get("/greet")
        self.assertEqual(resp.status, 400)

    @unittest_run_loop
    async def test_optional_param_uses_default(self) -> None:
        resp = await self.client.get("/maybe")
        self.assertEqual(resp.status, 200)
        self.assertEqual(await resp.text(), "Hello, Nobody")

    @unittest_run_loop
    async def test_optional_param_provided(self) -> None:
        resp = await self.client.get("/maybe?who=Kenny")
        self.assertEqual(resp.status, 200)
        self.assertEqual(await resp.text(), "Hello, Kenny")


if __name__ == "__main__":
    unittest.main()
