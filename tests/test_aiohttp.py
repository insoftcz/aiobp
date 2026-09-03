"""Unit tests for aiobp.aiohttp"""

import socket
import unittest
from io import BytesIO
from typing import Annotated, Any, Optional, TypedDict, Union
from unittest.mock import AsyncMock, MagicMock

import msgspec
from aiohttp import web as aioweb
from aiohttp.web import FileField
from aiohttp.test_utils import AioHTTPTestCase, make_mocked_request, unittest_run_loop
from msgspec import Meta
from multidict import MultiDict

from aiobp.aiohttp import (
    BodyKey,
    ClientAddress,
    CookieKey,
    FromBody,
    FromPath,
    FromQuery,
    HeaderKey,
    HttpRangeRequest,
    Param,
    PathKey,
    QueryKey,
    Router,
    ServerHostname,
    http_range,
    range_headers,
)
from aiobp.aiohttp._connection import get_client_address, get_server_hostname
from aiobp.aiohttp._provider import Provider

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_request(
    *,
    match_info: Optional[dict] = None,
    query: Optional[Union[dict, list[tuple[str, Any]]]] = None,
    headers: Optional[dict] = None,
    cookies: Optional[dict] = None,
    content_type: str = "",
    body: Optional[bytes] = None,
    json_body: object = None,
    post_data: Optional[Union[dict, list[tuple[str, Any]]]] = None,
    peername: Optional[tuple] = None,
) -> aioweb.Request:
    """Build a minimal mock aiohttp Request."""
    request = MagicMock(spec=aioweb.Request)
    request.match_info = match_info if match_info is not None else {}
    # Real MultiDict, not a plain dict, so getall() and duplicate keys behave like aiohttp.
    request.query = MultiDict(query) if query is not None else MultiDict()
    request.headers = headers if headers is not None else {}
    request.cookies = cookies if cookies is not None else {}
    request.content_type = content_type
    if peername is not None:
        transport = MagicMock()
        transport.get_extra_info.return_value = peername
        request.transport = transport
    else:
        request.transport = None
    if body is not None:
        request.read = AsyncMock(return_value=body)
    if json_body is not None:
        request.json = AsyncMock(return_value=json_body)
    if post_data is not None:
        # Real MultiDict, not a plain dict, so getall() and duplicate keys behave like aiohttp.
        request.post = AsyncMock(return_value=MultiDict(post_data))
    return request


# ---------------------------------------------------------------------------
# Provider unit tests (no HTTP server needed)
# ---------------------------------------------------------------------------

class TestProviderGetAnnotation(unittest.TestCase):

    def test_plain_annotated(self) -> None:
        hint = Annotated[str, Meta(description="x")]
        typ, optional, meta, source = Provider.get_annotation(hint)
        self.assertIs(typ, str)
        self.assertFalse(optional)
        self.assertIsInstance(meta, Meta)
        self.assertIsNone(source)

    def test_optional_annotated(self) -> None:
        hint = Optional[Annotated[str, Meta(description="x")]]
        typ, optional, meta, source = Provider.get_annotation(hint)
        self.assertIs(typ, str)
        self.assertTrue(optional)
        self.assertIsNone(source)

    def test_path_source_annotated(self) -> None:
        hint = Annotated[str, Meta(description="x"), PathKey]
        typ, optional, meta, source = Provider.get_annotation(hint)
        self.assertIs(typ, str)
        self.assertIs(source, PathKey)

    def test_query_source_annotated(self) -> None:
        hint = Annotated[str, Meta(description="x"), QueryKey]
        typ, optional, meta, source = Provider.get_annotation(hint)
        self.assertIs(typ, str)
        self.assertIs(source, QueryKey)

    def test_plain_type_raises(self) -> None:
        with self.assertRaises(TypeError):
            Provider.get_annotation(str)

    def test_source_without_meta(self) -> None:
        hint = Annotated[str, PathKey]
        typ, optional, meta, source = Provider.get_annotation(hint)
        self.assertIs(typ, str)
        self.assertIsNone(meta)
        self.assertIs(source, PathKey)

    def test_annotated_without_meta_or_source_raises(self) -> None:
        with self.assertRaises(TypeError):
            Provider.get_annotation(Annotated[str, "not a Meta"])

    def test_subscript_with_description(self) -> None:
        hint = PathKey[str, "someone"]
        typ, optional, meta, source = Provider.get_annotation(hint)
        self.assertIs(typ, str)
        self.assertFalse(optional)
        self.assertIsNotNone(meta)
        self.assertEqual(meta.description, "someone")
        self.assertEqual(source.kind, "path")

    def test_subscript_without_description(self) -> None:
        hint = QueryKey[int]
        typ, optional, meta, source = Provider.get_annotation(hint)
        self.assertIs(typ, int)
        self.assertIsNone(meta)
        self.assertEqual(source.kind, "query")

    def test_subscript_with_doc(self) -> None:
        from typing_extensions import Doc
        hint = PathKey[str, Doc("someone")]
        typ, optional, meta, source = Provider.get_annotation(hint)
        self.assertIs(typ, str)
        self.assertEqual(meta.description, "someone")
        self.assertEqual(source.kind, "path")

    def test_subscript_with_param(self) -> None:
        hint = PathKey[str, Param("someone", min_length=5)]
        typ, optional, meta, source = Provider.get_annotation(hint)
        self.assertIs(typ, str)
        self.assertEqual(meta.description, "someone")
        self.assertEqual(meta.min_length, 5)
        self.assertEqual(source.kind, "path")

    def test_subscript_optional(self) -> None:
        hint = Optional[QueryKey[str, "filter"]]
        typ, optional, meta, source = Provider.get_annotation(hint)
        self.assertIs(typ, str)
        self.assertTrue(optional)
        self.assertEqual(meta.description, "filter")
        self.assertEqual(source.kind, "query")

    def test_header_source_annotated(self) -> None:
        hint = HeaderKey[str, "auth token"]
        typ, optional, meta, source = Provider.get_annotation(hint)
        self.assertIs(typ, str)
        self.assertEqual(meta.description, "auth token")
        self.assertEqual(source.kind, "header")

    def test_cookie_source_annotated(self) -> None:
        hint = CookieKey[str, "session id"]
        typ, optional, meta, source = Provider.get_annotation(hint)
        self.assertIs(typ, str)
        self.assertEqual(meta.description, "session id")
        self.assertEqual(source.kind, "cookie")

    def test_param_source_override(self) -> None:
        hint = HeaderKey[str, Param("content type", source="Content-Type")]
        typ, optional, meta, source = Provider.get_annotation(hint)
        self.assertIs(typ, str)
        self.assertEqual(meta.extra, {"source": "Content-Type"})
        self.assertEqual(source.kind, "header")

    def test_from_path_is_whole_mapping_source(self) -> None:
        hint = FromPath[str, "paging"]
        typ, optional, meta, source = Provider.get_annotation(hint)
        self.assertEqual(source.kind, "path_items")

    def test_from_query_is_whole_mapping_source(self) -> None:
        hint = FromQuery[str, "paging"]
        typ, optional, meta, source = Provider.get_annotation(hint)
        self.assertEqual(source.kind, "query_items")

    def test_body_key_is_single_field_source(self) -> None:
        hint = BodyKey[str, "grant type"]
        typ, optional, meta, source = Provider.get_annotation(hint)
        self.assertEqual(source.kind, "body_key")


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

    async def test_path_source_ignores_query(self) -> None:
        async def handler(who: Annotated[str, Meta(description="name"), PathKey]) -> str: ...
        provider = self._make_provider(handler)
        request = make_request(query={"who": "from_query"})
        with self.assertRaises(TypeError):
            await provider.gather_args(request)

    async def test_path_source_resolves_from_path(self) -> None:
        async def handler(who: Annotated[str, Meta(description="name"), PathKey]) -> str: ...
        provider = self._make_provider(handler)
        request = make_request(match_info={"who": "world"})
        args = await provider.gather_args(request)
        self.assertEqual(args, {"who": "world"})

    async def test_query_source_ignores_path(self) -> None:
        async def handler(who: Annotated[str, Meta(description="name"), QueryKey]) -> str: ...
        provider = self._make_provider(handler)
        request = make_request(match_info={"who": "from_path"})
        with self.assertRaises(TypeError):
            await provider.gather_args(request)

    async def test_query_source_resolves_from_query(self) -> None:
        async def handler(who: Annotated[str, Meta(description="name"), QueryKey]) -> str: ...
        provider = self._make_provider(handler)
        request = make_request(query={"who": "world"})
        args = await provider.gather_args(request)
        self.assertEqual(args, {"who": "world"})

    async def test_query_bare_flag_falls_back_to_default_for_non_str_type(self) -> None:
        """?attachments (no "=value") parses to "" — not coercible to bool, so use the default."""
        async def handler(
            attachments: Annotated[bool, Param("include attachments?"), QueryKey] = False,
        ) -> bool: ...
        provider = self._make_provider(handler)
        request = make_request(query={"attachments": ""})
        args = await provider.gather_args(request)
        self.assertIs(args["attachments"], False)

    async def test_query_bare_flag_still_a_valid_empty_string(self) -> None:
        """An empty value is meaningful for str fields, so it's kept rather than defaulted."""
        async def handler(name: Annotated[str, Meta(description="name"), QueryKey] = "fallback") -> str: ...
        provider = self._make_provider(handler)
        request = make_request(query={"name": ""})
        args = await provider.gather_args(request)
        self.assertEqual(args["name"], "")

    async def test_header_source_resolves_from_header(self) -> None:
        async def handler(who: Annotated[str, Meta(description="name"), HeaderKey]) -> str: ...
        provider = self._make_provider(handler)
        request = make_request(headers={"who": "world"})
        args = await provider.gather_args(request)
        self.assertEqual(args, {"who": "world"})

    async def test_cookie_source_resolves_from_cookie(self) -> None:
        async def handler(who: Annotated[str, Meta(description="name"), CookieKey]) -> str: ...
        provider = self._make_provider(handler)
        request = make_request(cookies={"who": "world"})
        args = await provider.gather_args(request)
        self.assertEqual(args, {"who": "world"})

    async def test_header_source_with_custom_name(self) -> None:
        async def handler(
            content_type: HeaderKey[str, Param("content type", source="Content-Type")],
        ) -> str: ...
        provider = self._make_provider(handler)
        request = make_request(headers={"Content-Type": "application/json"})
        args = await provider.gather_args(request)
        self.assertEqual(args, {"content_type": "application/json"})

    async def test_cookie_source_with_custom_name(self) -> None:
        async def handler(session: CookieKey[str, Param("session id", source="session-id")]) -> str: ...
        provider = self._make_provider(handler)
        request = make_request(cookies={"session-id": "abc123"})
        args = await provider.gather_args(request)
        self.assertEqual(args, {"session": "abc123"})

    async def test_body_json_struct(self) -> None:
        class Item(msgspec.Struct):
            name: str
            price: float

        async def handler(item: Annotated[Item, Meta(description="item"), FromBody]) -> str: ...
        provider = self._make_provider(handler)
        request = make_request(
            content_type="application/json",
            body=msgspec.json.encode(Item(name="widget", price=9.99)),
        )
        args = await provider.gather_args(request)
        self.assertEqual(args["item"].name, "widget")
        self.assertAlmostEqual(args["item"].price, 9.99)

    async def test_body_form_struct(self) -> None:
        class Item(msgspec.Struct):
            name: str
            count: int

        async def handler(item: Annotated[Item, Meta(description="item"), FromBody]) -> str: ...
        provider = self._make_provider(handler)
        request = make_request(
            content_type="application/x-www-form-urlencoded",
            post_data={"name": "widget", "count": "5"},
        )
        args = await provider.gather_args(request)
        self.assertEqual(args["item"].name, "widget")
        self.assertEqual(args["item"].count, 5)

    async def test_body_form_struct_with_uploaded_file(self) -> None:
        class Upload(msgspec.Struct):
            name: str
            upload: FileField

        async def handler(item: Annotated[Upload, Meta(description="item"), FromBody]) -> str: ...
        provider = self._make_provider(handler)
        file_field = FileField(
            name="upload",
            filename="test.bin",
            file=BytesIO(b"file content"),
            content_type="application/octet-stream",
            headers=MagicMock(),
        )
        request = make_request(
            content_type="multipart/form-data",
            post_data={"name": "widget", "upload": file_field},
        )
        args = await provider.gather_args(request)
        self.assertEqual(args["item"].name, "widget")
        self.assertIs(args["item"].upload, file_field)
        self.assertEqual(args["item"].upload.file.read(), b"file content")

    async def test_body_form_struct_with_repeated_field_as_list(self) -> None:
        class Filters(msgspec.Struct):
            name: str
            tags: list[str]

        async def handler(item: Annotated[Filters, Meta(description="item"), FromBody]) -> str: ...
        provider = self._make_provider(handler)
        request = make_request(
            content_type="application/x-www-form-urlencoded",
            post_data=[("name", "widget"), ("tags", "a"), ("tags", "b"), ("tags", "c")],
        )
        args = await provider.gather_args(request)
        self.assertEqual(args["item"].name, "widget")
        self.assertEqual(args["item"].tags, ["a", "b", "c"])

    async def test_body_form_struct_with_single_repeated_field_still_a_list(self) -> None:
        class Filters(msgspec.Struct):
            tags: list[str]

        async def handler(item: Annotated[Filters, Meta(description="item"), FromBody]) -> str: ...
        provider = self._make_provider(handler)
        request = make_request(
            content_type="application/x-www-form-urlencoded",
            post_data=[("tags", "only-one")],
        )
        args = await provider.gather_args(request)
        self.assertEqual(args["item"].tags, ["only-one"])

    async def test_body_json_typeddict(self) -> None:
        class Item(TypedDict):
            name: str
            price: float

        async def handler(item: Annotated[Item, Meta(description="item"), FromBody]) -> str: ...
        provider = self._make_provider(handler)
        request = make_request(
            content_type="application/json",
            body=msgspec.json.encode({"name": "widget", "price": 9.99}),
        )
        args = await provider.gather_args(request)
        self.assertEqual(args["item"]["name"], "widget")
        self.assertAlmostEqual(args["item"]["price"], 9.99)

    async def test_body_form_typeddict(self) -> None:
        class Item(TypedDict):
            name: str
            count: int

        async def handler(item: Annotated[Item, Meta(description="item"), FromBody]) -> str: ...
        provider = self._make_provider(handler)
        request = make_request(
            content_type="application/x-www-form-urlencoded",
            post_data={"name": "widget", "count": "5"},
        )
        args = await provider.gather_args(request)
        self.assertEqual(args["item"]["name"], "widget")
        self.assertEqual(args["item"]["count"], 5)

    async def test_body_form_typeddict_with_uploaded_file(self) -> None:
        class Upload(TypedDict):
            name: str
            upload: FileField

        async def handler(item: Annotated[Upload, Meta(description="item"), FromBody]) -> str: ...
        provider = self._make_provider(handler)
        file_field = FileField(
            name="upload",
            filename="test.bin",
            file=BytesIO(b"file content"),
            content_type="application/octet-stream",
            headers=MagicMock(),
        )
        request = make_request(
            content_type="multipart/form-data",
            post_data={"name": "widget", "upload": file_field},
        )
        args = await provider.gather_args(request)
        self.assertEqual(args["item"]["name"], "widget")
        self.assertIs(args["item"]["upload"], file_field)

    async def test_body_form_typeddict_with_repeated_field_as_list(self) -> None:
        class Filters(TypedDict):
            name: str
            tags: list[str]

        async def handler(item: Annotated[Filters, Meta(description="item"), FromBody]) -> str: ...
        provider = self._make_provider(handler)
        request = make_request(
            content_type="application/x-www-form-urlencoded",
            post_data=[("name", "widget"), ("tags", "a"), ("tags", "b"), ("tags", "c")],
        )
        args = await provider.gather_args(request)
        self.assertEqual(args["item"]["name"], "widget")
        self.assertEqual(args["item"]["tags"], ["a", "b", "c"])

    async def test_from_query_struct(self) -> None:
        class Paging(msgspec.Struct):
            limit: int
            offset: int

        async def handler(paging: Annotated[Paging, Meta(description="paging"), FromQuery]) -> str: ...
        provider = self._make_provider(handler)
        request = make_request(query={"limit": "10", "offset": "0"})
        args = await provider.gather_args(request)
        self.assertEqual(args["paging"].limit, 10)
        self.assertEqual(args["paging"].offset, 0)

    async def test_from_query_typeddict(self) -> None:
        class Paging(TypedDict):
            limit: int
            offset: int

        async def handler(paging: Annotated[Paging, Meta(description="paging"), FromQuery]) -> str: ...
        provider = self._make_provider(handler)
        request = make_request(query={"limit": "10", "offset": "0"})
        args = await provider.gather_args(request)
        self.assertEqual(args["paging"]["limit"], 10)
        self.assertEqual(args["paging"]["offset"], 0)

    async def test_from_query_repeated_field_as_list(self) -> None:
        class Filters(TypedDict):
            tags: list[str]

        async def handler(filters: Annotated[Filters, Meta(description="filters"), FromQuery]) -> str: ...
        provider = self._make_provider(handler)
        request = make_request(query=[("tags", "a"), ("tags", "b")])
        args = await provider.gather_args(request)
        self.assertEqual(args["filters"]["tags"], ["a", "b"])

    async def test_from_query_optional_missing_returns_default(self) -> None:
        class Paging(TypedDict):
            limit: int

        async def handler(
            paging: Optional[Annotated[Paging, Meta(description="paging"), FromQuery]] = None,
        ) -> str: ...
        provider = self._make_provider(handler)
        request = make_request()
        args = await provider.gather_args(request)
        self.assertIsNone(args["paging"])

    async def test_from_path_struct(self) -> None:
        class Segments(TypedDict):
            user_id: int
            post_id: int

        async def handler(segments: Annotated[Segments, Meta(description="segments"), FromPath]) -> str: ...
        provider = self._make_provider(handler)
        request = make_request(match_info={"user_id": "1", "post_id": "2"})
        args = await provider.gather_args(request)
        self.assertEqual(args["segments"]["user_id"], 1)
        self.assertEqual(args["segments"]["post_id"], 2)

    async def test_body_bytes_raw(self) -> None:
        async def handler(data: Annotated[bytes, Meta(description="raw"), FromBody]) -> str: ...
        provider = self._make_provider(handler)
        request = make_request(
            content_type="application/octet-stream",
            body=b"raw binary data",
        )
        args = await provider.gather_args(request)
        self.assertEqual(args["data"], b"raw binary data")

    async def test_body_bytes_ignores_multipart_structure(self) -> None:
        """FromBody[bytes] always returns the raw body, even for multipart requests."""
        async def handler(upload: Annotated[bytes, Meta(description="file"), FromBody]) -> str: ...
        provider = self._make_provider(handler)
        request = make_request(
            content_type="multipart/form-data",
            body=b"--boundary\r\nraw multipart bytes\r\n--boundary--",
        )
        args = await provider.gather_args(request)
        self.assertEqual(args["upload"], b"--boundary\r\nraw multipart bytes\r\n--boundary--")

    async def test_body_json_scalar(self) -> None:
        async def handler(name: Annotated[str, Meta(description="name"), FromBody]) -> str: ...
        provider = self._make_provider(handler)
        request = make_request(
            content_type="application/json",
            body=msgspec.json.encode("widget"),
        )
        args = await provider.gather_args(request)
        self.assertEqual(args["name"], "widget")

    async def test_body_form_scalar_raises(self) -> None:
        """A scalar FromBody can't be built from a multi-field form — it's not "the whole body" as one value."""
        async def handler(count: Annotated[int, Meta(description="n"), FromBody]) -> str: ...
        provider = self._make_provider(handler)
        request = make_request(
            content_type="application/x-www-form-urlencoded",
            post_data={"count": "42"},
        )
        with self.assertRaises(TypeError):
            await provider.gather_args(request)

    async def test_multiple_body_args_decode_independently(self) -> None:
        """FromBody is no longer limited to one per handler — the body can be re-read/re-decoded."""
        class Item(msgspec.Struct):
            name: str

        async def handler(
            item: Annotated[Item, Meta(description="item"), FromBody],
            raw: Annotated[bytes, Meta(description="raw"), FromBody],
        ) -> str: ...

        provider = self._make_provider(handler)
        body = msgspec.json.encode(Item(name="widget"))
        request = make_request(content_type="application/json", body=body)
        args = await provider.gather_args(request)
        self.assertEqual(args["item"].name, "widget")
        self.assertEqual(args["raw"], body)

    async def test_body_key_json(self) -> None:
        async def handler(name: Annotated[str, Meta(description="name"), BodyKey]) -> str: ...
        provider = self._make_provider(handler)
        request = make_request(
            content_type="application/json",
            body=msgspec.json.encode({"name": "widget"}),
        )
        args = await provider.gather_args(request)
        self.assertEqual(args["name"], "widget")

    async def test_body_key_form(self) -> None:
        async def handler(count: Annotated[int, Meta(description="n"), BodyKey]) -> str: ...
        provider = self._make_provider(handler)
        request = make_request(
            content_type="application/x-www-form-urlencoded",
            post_data={"count": "42"},
        )
        args = await provider.gather_args(request)
        self.assertEqual(args["count"], 42)

    async def test_body_key_source_override(self) -> None:
        async def handler(
            grant_type: Annotated[str, Param("grant type", source="grant-type"), BodyKey],
        ) -> str: ...
        provider = self._make_provider(handler)
        request = make_request(
            content_type="application/json",
            body=msgspec.json.encode({"grant-type": "client_credentials"}),
        )
        args = await provider.gather_args(request)
        self.assertEqual(args["grant_type"], "client_credentials")

    async def test_body_key_missing_required_raises(self) -> None:
        async def handler(name: Annotated[str, Meta(description="name"), BodyKey]) -> str: ...
        provider = self._make_provider(handler)
        request = make_request(content_type="application/json", body=msgspec.json.encode({}))
        with self.assertRaises(TypeError):
            await provider.gather_args(request)

    async def test_body_key_optional_missing_returns_default(self) -> None:
        async def handler(
            name: Optional[Annotated[str, Meta(description="name"), BodyKey]] = "fallback",
        ) -> str: ...
        provider = self._make_provider(handler)
        request = make_request(content_type="application/json", body=msgspec.json.encode({}))
        args = await provider.gather_args(request)
        self.assertEqual(args["name"], "fallback")

    async def test_body_key_alongside_from_body(self) -> None:
        """BodyKey and FromBody can read the same body independently."""
        class Item(msgspec.Struct):
            name: str
            count: int

        async def handler(
            item: Annotated[Item, Meta(description="item"), FromBody],
            name: Annotated[str, Meta(description="name"), BodyKey],
        ) -> str: ...

        provider = self._make_provider(handler)
        request = make_request(
            content_type="application/json",
            body=msgspec.json.encode({"name": "widget", "count": 5}),
        )
        args = await provider.gather_args(request)
        self.assertEqual(args["item"].name, "widget")
        self.assertEqual(args["item"].count, 5)
        self.assertEqual(args["name"], "widget")

    async def test_param_constraint_enforced(self) -> None:
        async def handler(who: PathKey[str, Param("name", min_length=5)]) -> str: ...
        provider = self._make_provider(handler)
        request = make_request(match_info={"who": "abc"})
        with self.assertRaises(TypeError, msg="Invalid value for who"):
            await provider.gather_args(request)

    async def test_param_constraint_passes(self) -> None:
        async def handler(who: PathKey[str, Param("name", min_length=5)]) -> str: ...
        provider = self._make_provider(handler)
        request = make_request(match_info={"who": "world!"})
        args = await provider.gather_args(request)
        self.assertEqual(args, {"who": "world!"})

    async def test_param_numeric_constraint(self) -> None:
        async def handler(age: QueryKey[int, Param("age", ge=18)]) -> str: ...
        provider = self._make_provider(handler)
        request = make_request(query={"age": "15"})
        with self.assertRaises(TypeError):
            await provider.gather_args(request)

        request = make_request(query={"age": "21"})
        args = await provider.gather_args(request)
        self.assertEqual(args, {"age": 21})

    async def test_body_missing_required_raises(self) -> None:
        async def handler(name: Annotated[str, Meta(description="name"), FromBody]) -> str: ...
        provider = self._make_provider(handler)
        request = make_request(
            content_type="application/json",
            body=b"",
        )
        with self.assertRaises(TypeError):
            await provider.gather_args(request)

    async def test_body_optional_empty_json_returns_default(self) -> None:
        async def handler(
            name: Optional[Annotated[str, Meta(description="name"), FromBody]] = None,
        ) -> str: ...
        provider = self._make_provider(handler)
        request = make_request(content_type="application/json", body=b"")
        args = await provider.gather_args(request)
        self.assertIsNone(args["name"])

    async def test_body_optional_empty_json_returns_provided_default(self) -> None:
        async def handler(
            name: Optional[Annotated[str, Meta(description="name"), FromBody]] = "fallback",
        ) -> str: ...
        provider = self._make_provider(handler)
        request = make_request(content_type="application/json", body=b"")
        args = await provider.gather_args(request)
        self.assertEqual(args["name"], "fallback")

    async def test_body_optional_empty_form_returns_default(self) -> None:
        class Item(msgspec.Struct):
            name: str

        async def handler(
            item: Optional[Annotated[Item, Meta(description="item"), FromBody]] = None,
        ) -> str: ...
        provider = self._make_provider(handler)
        request = make_request(content_type="application/x-www-form-urlencoded", post_data={})
        args = await provider.gather_args(request)
        self.assertIsNone(args["item"])

    async def test_injected_request(self) -> None:
        async def handler(request: aioweb.Request) -> None: ...
        provider = self._make_provider(handler)
        mock_request = make_request()
        args = await provider.gather_args(mock_request)
        self.assertIs(args["request"], mock_request)

    async def test_injected_server_hostname(self) -> None:
        async def handler(hostname: ServerHostname) -> None: ...
        provider = Provider(handler, Router()._type_injectors)
        request = make_request(headers={"Host": "example.com"})
        args = await provider.gather_args(request)
        self.assertEqual(args["hostname"], "example.com")
        self.assertIsInstance(args["hostname"], ServerHostname)

    async def test_injected_client_address(self) -> None:
        async def handler(client: ClientAddress) -> None: ...
        provider = Provider(handler, Router()._type_injectors)
        request = make_request(peername=("203.0.113.5", 54321))
        args = await provider.gather_args(request)
        self.assertEqual(args["client"], "203.0.113.5:54321")
        self.assertIsInstance(args["client"], ClientAddress)


# ---------------------------------------------------------------------------
# ServerHostname resolution
# ---------------------------------------------------------------------------

class TestServerHostname(unittest.TestCase):

    def test_regular_hostname_is_returned_as_is(self) -> None:
        request = make_request(headers={"Host": "example.com"})
        self.assertEqual(get_server_hostname(request), "example.com")

    def test_hostname_with_port_is_returned_as_is(self) -> None:
        request = make_request(headers={"Host": "example.com:8080"})
        self.assertEqual(get_server_hostname(request), "example.com:8080")

    def test_ipv4_host_falls_back_to_fqdn(self) -> None:
        request = make_request(headers={"Host": "127.0.0.1"})
        self.assertEqual(get_server_hostname(request), socket.getfqdn())

    def test_ipv6_host_falls_back_to_fqdn(self) -> None:
        request = make_request(headers={"Host": "::1"})
        self.assertEqual(get_server_hostname(request), socket.getfqdn())

    def test_localhost_falls_back_to_fqdn(self) -> None:
        request = make_request(headers={"Host": "localhost"})
        self.assertEqual(get_server_hostname(request), socket.getfqdn())

    def test_missing_host_header_falls_back_to_fqdn(self) -> None:
        request = make_request()
        self.assertEqual(get_server_hostname(request), socket.getfqdn())

    def test_result_is_a_server_hostname(self) -> None:
        request = make_request(headers={"Host": "example.com"})
        self.assertIsInstance(get_server_hostname(request), ServerHostname)


# ---------------------------------------------------------------------------
# ClientAddress resolution
# ---------------------------------------------------------------------------

class TestClientAddress(unittest.TestCase):

    def test_direct_connection_uses_transport_peername(self) -> None:
        request = make_request(peername=("203.0.113.5", 54321))
        self.assertEqual(get_client_address(request), "203.0.113.5:54321")

    def test_ipv6_peername_tuple_is_unpacked(self) -> None:
        request = make_request(peername=("::1", 9999, 0, 0))
        self.assertEqual(get_client_address(request), "::1:9999")

    def test_forwarded_header_overrides_transport_peer_address(self) -> None:
        """Behind a reverse proxy, X-Forwarded-For is the real client, not the proxy."""
        request = make_request(
            headers={"X-Forwarded-For": "198.51.100.7"},
            peername=("127.0.0.1", 8080),
        )
        self.assertEqual(get_client_address(request), "198.51.100.7:8080")

    def test_missing_port_falls_back_to_random_tag(self) -> None:
        request = make_request(headers={"X-Forwarded-For": "198.51.100.7"})
        addr, _, port = get_client_address(request).partition(":")
        self.assertEqual(addr, "198.51.100.7")
        self.assertEqual(len(port), 4)
        self.assertTrue(port.isalpha())

    def test_result_is_a_client_address(self) -> None:
        request = make_request(peername=("203.0.113.5", 54321))
        self.assertIsInstance(get_client_address(request), ClientAddress)


# ---------------------------------------------------------------------------
# HTTP Range request support
# ---------------------------------------------------------------------------

class FakeAsyncFile:
    """Minimal async file-like double backed by an in-memory buffer."""

    def __init__(self, data: bytes) -> None:
        self._buf = BytesIO(data)

    async def seek(self, offset: int, whence: int = 0) -> int:
        return self._buf.seek(offset, whence)

    async def read(self, size: int = -1) -> bytes:
        return self._buf.read(size)


class FakeAsyncOpen:
    """Minimal async context manager double, mimicking ``aiofile.async_open``."""

    def __init__(self, data: bytes) -> None:
        self._data = data

    async def __aenter__(self) -> FakeAsyncFile:
        return FakeAsyncFile(self._data)

    async def __aexit__(self, *exc: object) -> None:
        pass


class FakeAiofileWrapper:
    """Mimics aiofile's real ``FileIOWrapperBase``: sync seek(offset) only, no whence, no return value."""

    def __init__(self, data: bytes) -> None:
        self._buf = BytesIO(data)

    def seek(self, offset: int) -> None:
        self._buf.seek(offset)

    async def read(self, size: int = -1) -> bytes:
        return self._buf.read(size)


class TestHttpRangeHelpers(unittest.TestCase):

    def test_http_range_no_start_returns_none(self) -> None:
        self.assertIsNone(http_range(slice(None, None)))

    def test_http_range_defaults_chunk_when_stop_missing(self) -> None:
        start, end = http_range(slice(0, None))
        self.assertEqual(start, 0)
        self.assertEqual(end, 32767)

    def test_http_range_explicit_bounds(self) -> None:
        self.assertEqual(http_range(slice(2, 5)), (2, 5))

    def test_range_headers_within_bounds(self) -> None:
        headers = range_headers((2, 5), total_length=20)
        self.assertEqual(headers["Accept-Ranges"], "bytes")
        self.assertEqual(headers["Content-Range"], "bytes 2-5/20")

    def test_range_headers_clamps_end(self) -> None:
        headers = range_headers((10, 999), total_length=20)
        self.assertEqual(headers["Content-Range"], "bytes 10-19/20")


class TestHttpRangeRequest(unittest.IsolatedAsyncioTestCase):

    def _make_provider(self, handler) -> Provider:
        return Provider(handler, {HttpRangeRequest: HttpRangeRequest})

    async def test_no_range_header_returns_full_body(self) -> None:
        async def handler(range: HttpRangeRequest) -> aioweb.StreamResponse:
            return range.slice_response(b"0123456789ABCDEFGHIJ")

        provider = self._make_provider(handler)
        request = make_request()
        args = await provider.gather_args(request)
        resp = await handler(**args)
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.body, b"0123456789ABCDEFGHIJ")

    async def test_range_header_returns_partial_bytes(self) -> None:
        async def handler(range: HttpRangeRequest) -> aioweb.StreamResponse:
            return range.slice_response(b"0123456789ABCDEFGHIJ")

        provider = self._make_provider(handler)
        request = make_request(headers={"Range": "bytes=2-5"})
        args = await provider.gather_args(request)
        resp = await handler(**args)
        self.assertEqual(resp.status, 206)
        self.assertEqual(resp.body, b"2345")

    async def test_slice_merges_custom_headers(self) -> None:
        async def handler(range: HttpRangeRequest) -> aioweb.StreamResponse:
            range.response_headers["X-Call-Id"] = "abc"
            return range.slice_response(b"0123456789ABCDEFGHIJ")

        provider = self._make_provider(handler)
        request = make_request(headers={"Range": "bytes=2-5"})
        args = await provider.gather_args(request)
        resp = await handler(**args)
        self.assertEqual(resp.status, 206)
        self.assertEqual(resp.headers["X-Call-Id"], "abc")
        self.assertEqual(resp.headers["Content-Range"], "bytes 2-5/20")

    async def test_bytes_range_exposes_start_and_end(self) -> None:
        async def handler(range: HttpRangeRequest) -> Optional[tuple[int, int]]:
            return range.bytes_range

        provider = self._make_provider(handler)
        request = make_request(headers={"Range": "bytes=2-5"})
        args = await provider.gather_args(request)
        self.assertEqual(await handler(**args), (2, 5))

    async def test_bytes_range_is_none_without_range_header(self) -> None:
        async def handler(range: HttpRangeRequest) -> Optional[tuple[int, int]]:
            return range.bytes_range

        provider = self._make_provider(handler)
        request = make_request()
        args = await provider.gather_args(request)
        self.assertIsNone(await handler(**args))

    async def test_chunk_wraps_pre_sliced_data(self) -> None:
        async def handler(range: HttpRangeRequest) -> aioweb.StreamResponse:
            data = "23456789"[:4]  # pretend a cache already sliced to bytes_range
            return range.chunk_response(data.encode(), total_length=20)

        provider = self._make_provider(handler)
        request = make_request(headers={"Range": "bytes=2-5"})
        args = await provider.gather_args(request)
        resp = await handler(**args)
        self.assertEqual(resp.status, 206)
        self.assertEqual(resp.body, b"2345")
        self.assertEqual(resp.headers["Content-Range"], "bytes 2-5/20")

    async def test_chunk_without_range_returns_full_body(self) -> None:
        async def handler(range: HttpRangeRequest) -> aioweb.StreamResponse:
            return range.chunk_response(b"0123456789ABCDEFGHIJ", total_length=20)

        provider = self._make_provider(handler)
        request = make_request()
        args = await provider.gather_args(request)
        resp = await handler(**args)
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.body, b"0123456789ABCDEFGHIJ")

    async def test_chunk_unsatisfiable_returns_416(self) -> None:
        async def handler(range: HttpRangeRequest) -> aioweb.StreamResponse:
            return range.chunk_response(b"", total_length=20)

        provider = self._make_provider(handler)
        request = make_request(headers={"Range": "bytes=100-200"})
        args = await provider.gather_args(request)
        resp = await handler(**args)
        self.assertEqual(resp.status, 416)
        self.assertEqual(resp.headers["Content-Range"], "bytes */20")

    async def test_range_header_returns_partial_text(self) -> None:
        async def handler(range: HttpRangeRequest) -> aioweb.StreamResponse:
            return range.slice_response("0123456789ABCDEFGHIJ")

        provider = self._make_provider(handler)
        request = make_request(headers={"Range": "bytes=2-5"})
        args = await provider.gather_args(request)
        resp = await handler(**args)
        self.assertEqual(resp.status, 206)
        self.assertEqual(resp.text, "2345")

    async def test_open_ended_range_clamps_to_content_length(self) -> None:
        async def handler(range: HttpRangeRequest) -> aioweb.StreamResponse:
            return range.slice_response(b"0123456789ABCDEFGHIJ")

        provider = self._make_provider(handler)
        request = make_request(headers={"Range": "bytes=15-"})
        args = await provider.gather_args(request)
        resp = await handler(**args)
        self.assertEqual(resp.status, 206)
        self.assertEqual(resp.body, b"FGHIJ")
        self.assertEqual(resp.headers["Content-Range"], "bytes 15-19/20")

    async def test_unsatisfiable_range_returns_416(self) -> None:
        async def handler(range: HttpRangeRequest) -> aioweb.StreamResponse:
            return range.slice_response(b"0123456789ABCDEFGHIJ")

        provider = self._make_provider(handler)
        request = make_request(headers={"Range": "bytes=100-200"})
        args = await provider.gather_args(request)
        resp = await handler(**args)
        self.assertEqual(resp.status, 416)
        self.assertEqual(resp.headers["Content-Range"], "bytes */20")

    async def test_malformed_range_header_serves_full_content(self) -> None:
        async def handler(range: HttpRangeRequest) -> aioweb.StreamResponse:
            return range.slice_response(b"0123456789")

        provider = self._make_provider(handler)
        request = make_request(headers={"Range": "not-a-range"})
        args = await provider.gather_args(request)
        resp = await handler(**args)
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.body, b"0123456789")


def _written_body(request: aioweb.Request) -> bytes:
    """Collect the bytes a streamed response wrote to the (mocked) payload writer."""
    calls = request._payload_writer.write.call_args_list  # noqa: SLF001
    return b"".join(call.args[0] for call in calls)


class TestHttpRangeRequestStream(unittest.IsolatedAsyncioTestCase):
    """``HttpRangeRequest.stream()`` writes bounded chunks instead of buffering the whole file."""

    def _make_provider(self, handler) -> Provider:
        return Provider(handler, {HttpRangeRequest: HttpRangeRequest})

    async def test_no_range_header_streams_full_file(self) -> None:
        async def handler(range: HttpRangeRequest) -> aioweb.StreamResponse:
            return await range.stream_response(FakeAsyncFile(b"0123456789ABCDEFGHIJ"))

        provider = self._make_provider(handler)
        request = make_mocked_request("GET", "/download")
        args = await provider.gather_args(request)
        resp = await handler(**args)
        self.assertEqual(resp.status, 200)
        self.assertEqual(_written_body(request), b"0123456789ABCDEFGHIJ")

    async def test_range_header_streams_partial_content(self) -> None:
        async def handler(range: HttpRangeRequest) -> aioweb.StreamResponse:
            return await range.stream_response(FakeAsyncFile(b"0123456789ABCDEFGHIJ"))

        provider = self._make_provider(handler)
        request = make_mocked_request("GET", "/download", headers={"Range": "bytes=2-5"})
        args = await provider.gather_args(request)
        resp = await handler(**args)
        self.assertEqual(resp.status, 206)
        self.assertEqual(_written_body(request), b"2345")
        self.assertEqual(resp.headers["Content-Range"], "bytes 2-5/20")

    async def test_stream_merges_custom_headers(self) -> None:
        async def handler(range: HttpRangeRequest) -> aioweb.StreamResponse:
            range.response_headers["X-Call-Id"] = "abc"
            return await range.stream_response(FakeAsyncFile(b"0123456789ABCDEFGHIJ"))

        provider = self._make_provider(handler)
        request = make_mocked_request("GET", "/download", headers={"Range": "bytes=2-5"})
        args = await provider.gather_args(request)
        resp = await handler(**args)
        self.assertEqual(resp.status, 206)
        self.assertEqual(resp.headers["X-Call-Id"], "abc")
        self.assertEqual(resp.headers["Content-Range"], "bytes 2-5/20")

    async def test_streams_from_async_context_manager(self) -> None:
        async def handler(range: HttpRangeRequest) -> aioweb.StreamResponse:
            async with FakeAsyncOpen(b"0123456789ABCDEFGHIJ") as file:
                return await range.stream_response(file)

        provider = self._make_provider(handler)
        request = make_mocked_request("GET", "/download", headers={"Range": "bytes=5-9"})
        args = await provider.gather_args(request)
        resp = await handler(**args)
        self.assertEqual(resp.status, 206)
        self.assertEqual(_written_body(request), b"56789")

    async def test_unsatisfiable_range_returns_416_without_streaming(self) -> None:
        async def handler(range: HttpRangeRequest) -> aioweb.StreamResponse:
            return await range.stream_response(FakeAsyncFile(b"0123456789ABCDEFGHIJ"))

        provider = self._make_provider(handler)
        request = make_mocked_request("GET", "/download", headers={"Range": "bytes=100-200"})
        args = await provider.gather_args(request)
        resp = await handler(**args)
        self.assertEqual(resp.status, 416)
        self.assertEqual(resp.headers["Content-Range"], "bytes */20")

    async def test_reads_in_bounded_chunks(self) -> None:
        """A file larger than the stream chunk size is written in more than one chunk."""
        big = b"x" * (65536 + 10)

        async def handler(range: HttpRangeRequest) -> aioweb.StreamResponse:
            return await range.stream_response(FakeAsyncFile(big))

        provider = self._make_provider(handler)
        request = make_mocked_request("GET", "/download")
        args = await provider.gather_args(request)
        resp = await handler(**args)
        self.assertEqual(resp.status, 200)
        calls = request._payload_writer.write.call_args_list  # noqa: SLF001
        self.assertGreater(len(calls), 1)
        self.assertEqual(_written_body(request), big)

    async def test_aiofile_style_wrapper_with_explicit_total_length(self) -> None:
        """aiofile's real seek() is sync, single-arg, and returns None — must still work."""
        async def handler(range: HttpRangeRequest) -> aioweb.StreamResponse:
            return await range.stream_response(FakeAiofileWrapper(b"0123456789ABCDEFGHIJ"), total_length=20)

        provider = self._make_provider(handler)
        request = make_mocked_request("GET", "/download", headers={"Range": "bytes=2-5"})
        args = await provider.gather_args(request)
        resp = await handler(**args)
        self.assertEqual(resp.status, 206)
        self.assertEqual(_written_body(request), b"2345")
        self.assertEqual(resp.headers["Content-Range"], "bytes 2-5/20")

    async def test_aiofile_style_wrapper_full_download_with_explicit_total_length(self) -> None:
        async def handler(range: HttpRangeRequest) -> aioweb.StreamResponse:
            return await range.stream_response(FakeAiofileWrapper(b"0123456789ABCDEFGHIJ"), total_length=20)

        provider = self._make_provider(handler)
        request = make_mocked_request("GET", "/download")
        args = await provider.gather_args(request)
        resp = await handler(**args)
        self.assertEqual(resp.status, 200)
        self.assertEqual(_written_body(request), b"0123456789ABCDEFGHIJ")

    async def test_missing_total_length_raises_clear_error_for_unsupported_seek(self) -> None:
        async def handler(range: HttpRangeRequest) -> aioweb.StreamResponse:
            return await range.stream_response(FakeAiofileWrapper(b"hello"))

        provider = self._make_provider(handler)
        request = make_mocked_request("GET", "/download")
        args = await provider.gather_args(request)
        with self.assertRaises(TypeError):
            await handler(**args)


# ---------------------------------------------------------------------------
# Router integration tests (real aiohttp test server)
# ---------------------------------------------------------------------------

def build_app() -> aioweb.Application:
    """Create a fresh app with its own router for each test."""
    router = Router()

    @router.api.get("/hello/{who}")
    async def hello(who: Annotated[str, Meta(description="name")]) -> str:
        return f"Hello, {who}"

    @router.api.get("/greet")
    async def greet(who: Annotated[str, Meta(description="name")]) -> str:
        return f"Hello, {who}"

    @router.api.get("/maybe")
    async def maybe(who: Optional[Annotated[str, Meta(description="name")]] = "Nobody") -> str:
        return f"Hello, {who or 'Nobody'}"

    @router.get("/plain/{who}")
    async def plain_greet(who: Annotated[str, PathKey]) -> str:
        return f"Hi, {who}"

    @router.api.get("/short/{who}")
    async def short_greet(who: PathKey[str, "someone"]) -> str:
        return f"Hey, {who}"

    @router.get("/old")
    async def old_page() -> None:
        raise aioweb.HTTPFound("/hello/world")

    @router.api.get("/gone", tag="Errors")
    async def gone() -> None:
        raise aioweb.HTTPGone(text="This resource is gone")

    @router.get("/download")
    def download(range: HttpRangeRequest) -> aioweb.StreamResponse:
        return range.slice_response(b"0123456789ABCDEFGHIJ")

    @router.get("/download-stream")
    def download_stream(range: HttpRangeRequest) -> aioweb.StreamResponse:
        return range.stream_response(FakeAsyncFile(b"0123456789ABCDEFGHIJ"))

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
        self.assertEqual(await resp.json(), "Hello, world")
        self.assertEqual(resp.content_type, "application/json")

    @unittest_run_loop
    async def test_query_param(self) -> None:
        resp = await self.client.get("/greet?who=world")
        self.assertEqual(resp.status, 200)
        self.assertEqual(await resp.json(), "Hello, world")

    @unittest_run_loop
    async def test_missing_required_param_returns_400(self) -> None:
        resp = await self.client.get("/greet")
        self.assertEqual(resp.status, 400)

    @unittest_run_loop
    async def test_optional_param_uses_default(self) -> None:
        resp = await self.client.get("/maybe")
        self.assertEqual(resp.status, 200)
        self.assertEqual(await resp.json(), "Hello, Nobody")

    @unittest_run_loop
    async def test_optional_param_provided(self) -> None:
        resp = await self.client.get("/maybe?who=Kenny")
        self.assertEqual(resp.status, 200)
        self.assertEqual(await resp.json(), "Hello, Kenny")

    @unittest_run_loop
    async def test_plain_route_without_meta(self) -> None:
        resp = await self.client.get("/plain/world")
        self.assertEqual(resp.status, 200)
        self.assertEqual(await resp.text(), "Hi, world")

    @unittest_run_loop
    async def test_subscript_route(self) -> None:
        resp = await self.client.get("/short/world")
        self.assertEqual(resp.status, 200)
        self.assertEqual(await resp.json(), "Hey, world")

    @unittest_run_loop
    async def test_redirect(self) -> None:
        resp = await self.client.get("/old", allow_redirects=False)
        self.assertEqual(resp.status, 302)
        self.assertEqual(resp.headers["Location"], "/hello/world")

    @unittest_run_loop
    async def test_redirect_follows(self) -> None:
        resp = await self.client.get("/old")
        self.assertEqual(resp.status, 200)
        self.assertEqual(await resp.json(), "Hello, world")

    @unittest_run_loop
    async def test_http_error(self) -> None:
        resp = await self.client.get("/gone")
        self.assertEqual(resp.status, 410)
        self.assertEqual(await resp.text(), "This resource is gone")

    @unittest_run_loop
    async def test_download_without_range_returns_full_body(self) -> None:
        resp = await self.client.get("/download")
        self.assertEqual(resp.status, 200)
        self.assertEqual(await resp.read(), b"0123456789ABCDEFGHIJ")

    @unittest_run_loop
    async def test_download_with_range_returns_partial_content(self) -> None:
        resp = await self.client.get("/download", headers={"Range": "bytes=2-5"})
        self.assertEqual(resp.status, 206)
        self.assertEqual(await resp.read(), b"2345")
        self.assertEqual(resp.headers["Content-Range"], "bytes 2-5/20")

    @unittest_run_loop
    async def test_download_stream_without_range_returns_full_body(self) -> None:
        resp = await self.client.get("/download-stream")
        self.assertEqual(resp.status, 200)
        self.assertEqual(await resp.read(), b"0123456789ABCDEFGHIJ")

    @unittest_run_loop
    async def test_download_stream_with_range_returns_partial_content(self) -> None:
        resp = await self.client.get("/download-stream", headers={"Range": "bytes=2-5"})
        self.assertEqual(resp.status, 206)
        self.assertEqual(await resp.read(), b"2345")
        self.assertEqual(resp.headers["Content-Range"], "bytes 2-5/20")


def build_app_with_output_handlers() -> aioweb.Application:
    """Create an app whose ApiRouter wraps successes/errors in an envelope."""
    router = Router(
        on_result=lambda value: {"data": value},
        on_error=lambda error: {"error": str(error)},
    )

    @router.api.get("/greet")
    async def greet(who: Annotated[str, Meta(description="name")]) -> str:
        return f"Hello, {who}"

    @router.api.get("/boom")
    async def boom() -> str:
        msg = "kaboom"
        raise ValueError(msg)

    @router.api.get("/gone")
    async def gone() -> None:
        raise aioweb.HTTPGone(text="This resource is gone")

    app = aioweb.Application()
    app.add_routes(router)
    return app


class TestApiRouterOutputHandlers(AioHTTPTestCase):

    async def get_application(self) -> aioweb.Application:
        return build_app_with_output_handlers()

    @unittest_run_loop
    async def test_on_result_wraps_successful_response(self) -> None:
        resp = await self.client.get("/greet?who=world")
        self.assertEqual(resp.status, 200)
        self.assertEqual(await resp.json(), {"data": "Hello, world"})

    @unittest_run_loop
    async def test_on_error_wraps_unhandled_exception(self) -> None:
        resp = await self.client.get("/boom")
        self.assertEqual(resp.status, 500)
        self.assertEqual(await resp.json(), {"error": "kaboom"})

    @unittest_run_loop
    async def test_deliberate_http_exception_bypasses_on_error(self) -> None:
        resp = await self.client.get("/gone")
        self.assertEqual(resp.status, 410)
        self.assertEqual(await resp.text(), "This resource is gone")


def build_app_with_late_bound_output_handler() -> aioweb.Application:
    """Set router.api.on_result after the route is decorated but before build() runs."""
    router = Router()

    @router.api.get("/greet")
    async def greet(who: Annotated[str, Meta(description="name")]) -> str:
        return f"Hello, {who}"

    router.api.on_result = lambda value: {"data": value}

    app = aioweb.Application()
    app.add_routes(router)
    return app


class TestApiRouterOutputHandlerSetAfterDecoration(AioHTTPTestCase):

    async def get_application(self) -> aioweb.Application:
        return build_app_with_late_bound_output_handler()

    @unittest_run_loop
    async def test_on_result_set_after_decoration_still_applies(self) -> None:
        resp = await self.client.get("/greet?who=world")
        self.assertEqual(resp.status, 200)
        self.assertEqual(await resp.json(), {"data": "Hello, world"})


class TestDuplicateRoute(unittest.TestCase):

    def test_duplicate_api_route_raises(self) -> None:
        router = Router()

        @router.api.get("/items")
        async def list_items(request: aioweb.Request) -> None: ...

        @router.api.get("/items")
        async def list_items_again(request: aioweb.Request) -> None: ...

        with self.assertRaises(ValueError, msg="Duplicate route: GET /items"):
            router.build()

    def test_duplicate_plain_route_raises(self) -> None:
        router = Router()

        @router.get("/page")
        async def page(request: aioweb.Request) -> None: ...

        @router.get("/page")
        async def page_again(request: aioweb.Request) -> None: ...

        with self.assertRaises(ValueError, msg="Duplicate route: GET /page"):
            router.build()

    def test_duplicate_across_api_and_plain_raises(self) -> None:
        router = Router()

        @router.api.get("/shared")
        async def api_handler(request: aioweb.Request) -> None: ...

        @router.get("/shared")
        async def plain_handler(request: aioweb.Request) -> None: ...

        with self.assertRaises(ValueError, msg="Duplicate route: GET /shared"):
            router.build()

    def test_same_path_different_methods_allowed(self) -> None:
        router = Router()

        @router.api.get("/items")
        async def list_items(request: aioweb.Request) -> None: ...

        @router.api.post("/items")
        async def create_item(request: aioweb.Request) -> None: ...

        router.build()  # should not raise

    def test_duplicate_via_include_raises(self) -> None:
        router = Router()

        class A:
            @router.api.get("/hello")
            async def hello(self, request: aioweb.Request) -> None: ...

        class B:
            @router.api.get("/hello")
            async def hello(self, request: aioweb.Request) -> None: ...

        router.include(A())
        router.include(B())

        with self.assertRaises(ValueError, msg="Duplicate route: GET /hello"):
            router.build()


# ---------------------------------------------------------------------------
# Pending route collection tests
# ---------------------------------------------------------------------------

class TestPendingRoutes(unittest.TestCase):

    def test_api_get_stores_metadata(self) -> None:
        router = Router()

        @router.api.get('/items/{id}', tag='Items')
        async def get_item() -> None: ...

        self.assertEqual(len(router._pending), 1)
        entry = router._pending[0]
        self.assertEqual(entry.method, 'GET')
        self.assertEqual(entry.path, '/items/{id}')
        self.assertEqual(entry.router_type, 'api')
        self.assertEqual(entry.tag, 'Items')
        self.assertIs(entry.handler, get_item)

    def test_plain_post_stores_metadata(self) -> None:
        router = Router()

        @router.post('/submit')
        async def submit() -> None: ...

        self.assertEqual(len(router._pending), 1)
        entry = router._pending[0]
        self.assertEqual(entry.method, 'POST')
        self.assertEqual(entry.router_type, 'plain')

    def test_multiple_decorators_on_same_handler(self) -> None:
        router = Router()

        @router.api.get('/items')
        @router.api.get('/all-items')
        async def list_items() -> None: ...

        self.assertEqual(len(router._pending), 2)


# ---------------------------------------------------------------------------
# Unbound self detection
# ---------------------------------------------------------------------------

class TestUnboundSelfDetection(unittest.TestCase):

    def test_missing_include_raises(self) -> None:
        router = Router()

        class Greeter:
            @router.api.get('/greet')
            async def hello(self) -> str:
                return 'hi'

        with self.assertRaises(TypeError, msg='unbound "self"'):
            router.build()


# ---------------------------------------------------------------------------
# Router.include() integration tests (stateful — real aiohttp test server)
# ---------------------------------------------------------------------------

class TestRouterInclude(AioHTTPTestCase):

    async def get_application(self) -> aioweb.Application:
        router = Router()

        class Greeter:
            @router.api.get('/greet/{who}')
            async def hello(self, who: Annotated[str, Meta(description='name')]) -> str:
                return f'Hello, {who}'

            @router.api.get('/greet')
            async def list_all(self) -> str:
                return 'everyone'

            @router.get('/greet/page')
            async def page(self) -> str:
                return '<h1>Hi</h1>'

        class AbsoluteRoutes:
            @router.api.get('/absolute/path')
            async def absolute(self) -> str:
                return 'absolute'

        router.include(Greeter())
        router.include(AbsoluteRoutes())

        app = aioweb.Application()
        app.add_routes(router)
        return app

    @unittest_run_loop
    async def test_include_path_param(self) -> None:
        resp = await self.client.get('/greet/world')
        self.assertEqual(resp.status, 200)
        self.assertEqual(await resp.json(), 'Hello, world')

    @unittest_run_loop
    async def test_include_prefix_only(self) -> None:
        resp = await self.client.get('/greet')
        self.assertEqual(resp.status, 200)
        self.assertEqual(await resp.json(), 'everyone')

    @unittest_run_loop
    async def test_include_html_route(self) -> None:
        resp = await self.client.get('/greet/page')
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.content_type, 'text/html')
        self.assertEqual(await resp.text(), '<h1>Hi</h1>')

    @unittest_run_loop
    async def test_include_absolute_path(self) -> None:
        resp = await self.client.get('/absolute/path')
        self.assertEqual(resp.status, 200)
        self.assertEqual(await resp.json(), 'absolute')

    @unittest_run_loop
    async def test_include_with_type_injector(self) -> None:
        """Verify that type injectors work with include()."""
        router = Router()

        class MyService:
            value = 42

        class Handler:
            @router.api.get('/injected')
            async def handle(self, svc: MyService) -> str:
                return str(svc.value)

        router.add_type_injector(MyService, lambda request: MyService())
        router.include(Handler())

        app = aioweb.Application()
        app.add_routes(router)

        from aiohttp.test_utils import TestServer, TestClient
        server = TestServer(app)
        client = TestClient(server)
        await client.start_server()
        try:
            resp = await client.get('/injected')
            self.assertEqual(resp.status, 200)
            self.assertEqual(await resp.json(), '42')
        finally:
            await client.close()

if __name__ == "__main__":
    unittest.main()
