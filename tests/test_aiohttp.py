"""Unit tests for aiobp.aiohttp"""

import socket
import sys
import types
from io import BytesIO
from typing import Annotated, Any, Optional, TypedDict, Union
from unittest.mock import AsyncMock, MagicMock

import msgspec
import pytest
from aiohttp import web as aioweb
from aiohttp.test_utils import make_mocked_request
from aiohttp.web import FileField
from msgspec import Meta
from multidict import MultiDict
from yarl import URL

from aiobp.aiohttp import (
    ApiError,
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
from aiobp.aiohttp._openapi import OpenAPIBuilder
from aiobp.aiohttp._provider import ArgumentError, Provider, RequestValidationError, SourceKind

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

class TestProviderGetAnnotation:

    def test_plain_annotated(self) -> None:
        hint = Annotated[str, Meta(description="x")]
        typ, optional, meta, source = Provider.get_annotation(hint)
        assert typ is str
        assert not optional
        assert isinstance(meta, Meta)
        assert source is None

    def test_optional_annotated(self) -> None:
        hint = Optional[Annotated[str, Meta(description="x")]]
        typ, optional, meta, source = Provider.get_annotation(hint)
        assert typ is str
        assert optional
        assert source is None

    def test_path_source_annotated(self) -> None:
        hint = Annotated[str, Meta(description="x"), PathKey]
        typ, optional, meta, source = Provider.get_annotation(hint)
        assert typ is str
        assert source is PathKey

    def test_query_source_annotated(self) -> None:
        hint = Annotated[str, Meta(description="x"), QueryKey]
        typ, optional, meta, source = Provider.get_annotation(hint)
        assert typ is str
        assert source is QueryKey

    def test_plain_type_raises(self) -> None:
        with pytest.raises(TypeError):
            Provider.get_annotation(str)

    def test_source_without_meta(self) -> None:
        hint = Annotated[str, PathKey]
        typ, optional, meta, source = Provider.get_annotation(hint)
        assert typ is str
        assert meta is None
        assert source is PathKey

    def test_annotated_without_meta_or_source_raises(self) -> None:
        with pytest.raises(TypeError):
            Provider.get_annotation(Annotated[str, "not a Meta"])

    def test_subscript_with_description(self) -> None:
        hint = PathKey[str, "someone"]
        typ, optional, meta, source = Provider.get_annotation(hint)
        assert typ is str
        assert not optional
        assert meta is not None
        assert meta.description == "someone"
        assert source.kind == "path"

    def test_subscript_without_description(self) -> None:
        hint = QueryKey[int]
        typ, optional, meta, source = Provider.get_annotation(hint)
        assert typ is int
        assert meta is None
        assert source.kind == "query"

    def test_subscript_with_doc(self) -> None:
        from typing_extensions import Doc
        hint = PathKey[str, Doc("someone")]
        typ, optional, meta, source = Provider.get_annotation(hint)
        assert typ is str
        assert meta.description == "someone"
        assert source.kind == "path"

    def test_subscript_with_param(self) -> None:
        hint = PathKey[str, Param("someone", min_length=5)]
        typ, optional, meta, source = Provider.get_annotation(hint)
        assert typ is str
        assert meta.description == "someone"
        assert meta.min_length == 5
        assert source.kind == "path"

    def test_subscript_optional(self) -> None:
        hint = Optional[QueryKey[str, "filter"]]
        typ, optional, meta, source = Provider.get_annotation(hint)
        assert typ is str
        assert optional
        assert meta.description == "filter"
        assert source.kind == "query"

    def test_header_source_annotated(self) -> None:
        hint = HeaderKey[str, "auth token"]
        typ, optional, meta, source = Provider.get_annotation(hint)
        assert typ is str
        assert meta.description == "auth token"
        assert source.kind == "header"

    def test_cookie_source_annotated(self) -> None:
        hint = CookieKey[str, "session id"]
        typ, optional, meta, source = Provider.get_annotation(hint)
        assert typ is str
        assert meta.description == "session id"
        assert source.kind == "cookie"

    def test_param_source_override(self) -> None:
        hint = HeaderKey[str, Param("content type", source="Content-Type")]
        typ, optional, meta, source = Provider.get_annotation(hint)
        assert typ is str
        assert meta.extra == {"source": "Content-Type"}
        assert source.kind == "header"

    def test_from_path_is_whole_mapping_source(self) -> None:
        hint = FromPath[str, "paging"]
        typ, optional, meta, source = Provider.get_annotation(hint)
        assert source.kind == "path_items"

    def test_from_query_is_whole_mapping_source(self) -> None:
        hint = FromQuery[str, "paging"]
        typ, optional, meta, source = Provider.get_annotation(hint)
        assert source.kind == "query_items"

    def test_body_key_is_single_field_source(self) -> None:
        hint = BodyKey[str, "grant type"]
        typ, optional, meta, source = Provider.get_annotation(hint)
        assert source.kind == "body_key"


class TestProviderGatherArgs:

    def _make_provider(self, handler, injectors=None) -> Provider:
        return Provider(handler, injectors or {aioweb.Request: lambda request: request})

    async def test_path_argument(self) -> None:
        async def handler(who: Annotated[str, Meta(description="name")]) -> str: ...
        provider = self._make_provider(handler)
        request = make_request(match_info={"who": "world"})
        args = await provider.gather_args(request)
        assert args == {"who": "world"}

    async def test_query_argument(self) -> None:
        async def handler(who: Annotated[str, Meta(description="name")]) -> str: ...
        provider = self._make_provider(handler)
        request = make_request(query={"who": "world"})
        args = await provider.gather_args(request)
        assert args == {"who": "world"}

    async def test_missing_required_raises(self) -> None:
        async def handler(who: Annotated[str, Meta(description="name")]) -> str: ...
        provider = self._make_provider(handler)
        request = make_request()
        with pytest.raises(TypeError):
            await provider.gather_args(request)

    async def test_missing_required_raises_structured_error_with_source(self) -> None:
        async def handler(who: Annotated[str, Meta(description="name"), QueryKey]) -> str: ...
        provider = self._make_provider(handler)
        request = make_request()
        with pytest.raises(RequestValidationError) as ctx:
            await provider.gather_args(request)
        errors = ctx.value.errors
        assert len(errors) == 1
        assert isinstance(errors[0], ArgumentError)
        assert errors[0].attribute == "who"
        assert errors[0].source == SourceKind.QUERY
        assert errors[0].error == "Missing required value"

    async def test_multiple_failing_arguments_are_all_reported(self) -> None:
        async def handler(
            who: Annotated[str, Meta(description="name"), QueryKey],
            count: Annotated[int, Meta(description="n"), HeaderKey],
        ) -> str: ...
        provider = self._make_provider(handler)
        request = make_request()
        with pytest.raises(RequestValidationError) as ctx:
            await provider.gather_args(request)
        attributes = {error.attribute for error in ctx.value.errors}
        assert attributes == {"who", "count"}

    async def test_invalid_value_reports_body_source_for_annotated_body(self) -> None:
        async def handler(item: FromBody[dict, "payload"]) -> dict: ...
        provider = self._make_provider(handler)
        request = make_request(content_type="application/json", body=b"")
        with pytest.raises(RequestValidationError) as ctx:
            await provider.gather_args(request)
        assert ctx.value.errors[0].source == SourceKind.BODY

    async def test_optional_defaults_to_none(self) -> None:
        async def handler(who: Optional[Annotated[str, Meta(description="name")]] = None) -> Optional[str]: ...
        provider = self._make_provider(handler)
        request = make_request()
        args = await provider.gather_args(request)
        assert args["who"] is None

    async def test_optional_with_default(self) -> None:
        async def handler(who: Optional[Annotated[str, Meta(description="name")]] = "Nobody") -> Optional[str]: ...
        provider = self._make_provider(handler)
        request = make_request()
        args = await provider.gather_args(request)
        assert args["who"] == "Nobody"

    async def test_type_coercion_to_int(self) -> None:
        async def handler(count: Annotated[int, Meta(description="n")]) -> int: ...
        provider = self._make_provider(handler)
        request = make_request(query={"count": "42"})
        args = await provider.gather_args(request)
        assert args["count"] == 42
        assert isinstance(args["count"], int)

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
        assert args["svc"] is instance

    async def test_path_source_ignores_query(self) -> None:
        async def handler(who: Annotated[str, Meta(description="name"), PathKey]) -> str: ...
        provider = self._make_provider(handler)
        request = make_request(query={"who": "from_query"})
        with pytest.raises(TypeError):
            await provider.gather_args(request)

    async def test_path_source_resolves_from_path(self) -> None:
        async def handler(who: Annotated[str, Meta(description="name"), PathKey]) -> str: ...
        provider = self._make_provider(handler)
        request = make_request(match_info={"who": "world"})
        args = await provider.gather_args(request)
        assert args == {"who": "world"}

    async def test_query_source_ignores_path(self) -> None:
        async def handler(who: Annotated[str, Meta(description="name"), QueryKey]) -> str: ...
        provider = self._make_provider(handler)
        request = make_request(match_info={"who": "from_path"})
        with pytest.raises(TypeError):
            await provider.gather_args(request)

    async def test_query_source_resolves_from_query(self) -> None:
        async def handler(who: Annotated[str, Meta(description="name"), QueryKey]) -> str: ...
        provider = self._make_provider(handler)
        request = make_request(query={"who": "world"})
        args = await provider.gather_args(request)
        assert args == {"who": "world"}

    async def test_query_bare_flag_falls_back_to_default_for_non_str_type(self) -> None:
        """?attachments (no "=value") parses to "" — not coercible to bool, so use the default."""
        async def handler(
            attachments: Annotated[bool, Param("include attachments?"), QueryKey] = False,
        ) -> bool: ...
        provider = self._make_provider(handler)
        request = make_request(query={"attachments": ""})
        args = await provider.gather_args(request)
        assert args["attachments"] is False

    async def test_query_bare_flag_still_a_valid_empty_string(self) -> None:
        """An empty value is meaningful for str fields, so it's kept rather than defaulted."""
        async def handler(name: Annotated[str, Meta(description="name"), QueryKey] = "fallback") -> str: ...
        provider = self._make_provider(handler)
        request = make_request(query={"name": ""})
        args = await provider.gather_args(request)
        assert args["name"] == ""

    async def test_header_source_resolves_from_header(self) -> None:
        async def handler(who: Annotated[str, Meta(description="name"), HeaderKey]) -> str: ...
        provider = self._make_provider(handler)
        request = make_request(headers={"who": "world"})
        args = await provider.gather_args(request)
        assert args == {"who": "world"}

    async def test_cookie_source_resolves_from_cookie(self) -> None:
        async def handler(who: Annotated[str, Meta(description="name"), CookieKey]) -> str: ...
        provider = self._make_provider(handler)
        request = make_request(cookies={"who": "world"})
        args = await provider.gather_args(request)
        assert args == {"who": "world"}

    async def test_header_source_with_custom_name(self) -> None:
        async def handler(
            content_type: HeaderKey[str, Param("content type", source="Content-Type")],
        ) -> str: ...
        provider = self._make_provider(handler)
        request = make_request(headers={"Content-Type": "application/json"})
        args = await provider.gather_args(request)
        assert args == {"content_type": "application/json"}

    async def test_cookie_source_with_custom_name(self) -> None:
        async def handler(session: CookieKey[str, Param("session id", source="session-id")]) -> str: ...
        provider = self._make_provider(handler)
        request = make_request(cookies={"session-id": "abc123"})
        args = await provider.gather_args(request)
        assert args == {"session": "abc123"}

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
        assert args["item"].name == "widget"
        assert args["item"].price == pytest.approx(9.99)

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
        assert args["item"].name == "widget"
        assert args["item"].count == 5

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
        assert args["item"].name == "widget"
        assert args["item"].upload is file_field
        assert args["item"].upload.file.read() == b"file content"

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
        assert args["item"].name == "widget"
        assert args["item"].tags == ["a", "b", "c"]

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
        assert args["item"].tags == ["only-one"]

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
        assert args["item"]["name"] == "widget"
        assert args["item"]["price"] == pytest.approx(9.99)

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
        assert args["item"]["name"] == "widget"
        assert args["item"]["count"] == 5

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
        assert args["item"]["name"] == "widget"
        assert args["item"]["upload"] is file_field

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
        assert args["item"]["name"] == "widget"
        assert args["item"]["tags"] == ["a", "b", "c"]

    async def test_from_query_struct(self) -> None:
        class Paging(msgspec.Struct):
            limit: int
            offset: int

        async def handler(paging: Annotated[Paging, Meta(description="paging"), FromQuery]) -> str: ...
        provider = self._make_provider(handler)
        request = make_request(query={"limit": "10", "offset": "0"})
        args = await provider.gather_args(request)
        assert args["paging"].limit == 10
        assert args["paging"].offset == 0

    async def test_from_query_typeddict(self) -> None:
        class Paging(TypedDict):
            limit: int
            offset: int

        async def handler(paging: Annotated[Paging, Meta(description="paging"), FromQuery]) -> str: ...
        provider = self._make_provider(handler)
        request = make_request(query={"limit": "10", "offset": "0"})
        args = await provider.gather_args(request)
        assert args["paging"]["limit"] == 10
        assert args["paging"]["offset"] == 0

    async def test_from_query_repeated_field_as_list(self) -> None:
        class Filters(TypedDict):
            tags: list[str]

        async def handler(filters: Annotated[Filters, Meta(description="filters"), FromQuery]) -> str: ...
        provider = self._make_provider(handler)
        request = make_request(query=[("tags", "a"), ("tags", "b")])
        args = await provider.gather_args(request)
        assert args["filters"]["tags"] == ["a", "b"]

    async def test_from_query_optional_missing_returns_default(self) -> None:
        class Paging(TypedDict):
            limit: int

        async def handler(
            paging: Optional[Annotated[Paging, Meta(description="paging"), FromQuery]] = None,
        ) -> str: ...
        provider = self._make_provider(handler)
        request = make_request()
        args = await provider.gather_args(request)
        assert args["paging"] is None

    async def test_from_path_struct(self) -> None:
        class Segments(TypedDict):
            user_id: int
            post_id: int

        async def handler(segments: Annotated[Segments, Meta(description="segments"), FromPath]) -> str: ...
        provider = self._make_provider(handler)
        request = make_request(match_info={"user_id": "1", "post_id": "2"})
        args = await provider.gather_args(request)
        assert args["segments"]["user_id"] == 1
        assert args["segments"]["post_id"] == 2

    async def test_body_bytes_raw(self) -> None:
        async def handler(data: Annotated[bytes, Meta(description="raw"), FromBody]) -> str: ...
        provider = self._make_provider(handler)
        request = make_request(
            content_type="application/octet-stream",
            body=b"raw binary data",
        )
        args = await provider.gather_args(request)
        assert args["data"] == b"raw binary data"

    async def test_body_bytes_ignores_multipart_structure(self) -> None:
        """FromBody[bytes] always returns the raw body, even for multipart requests."""
        async def handler(upload: Annotated[bytes, Meta(description="file"), FromBody]) -> str: ...
        provider = self._make_provider(handler)
        request = make_request(
            content_type="multipart/form-data",
            body=b"--boundary\r\nraw multipart bytes\r\n--boundary--",
        )
        args = await provider.gather_args(request)
        assert args["upload"] == b"--boundary\r\nraw multipart bytes\r\n--boundary--"

    async def test_body_json_scalar(self) -> None:
        async def handler(name: Annotated[str, Meta(description="name"), FromBody]) -> str: ...
        provider = self._make_provider(handler)
        request = make_request(
            content_type="application/json",
            body=msgspec.json.encode("widget"),
        )
        args = await provider.gather_args(request)
        assert args["name"] == "widget"

    async def test_body_form_scalar_raises(self) -> None:
        """A scalar FromBody can't be built from a multi-field form — it's not "the whole body" as one value."""
        async def handler(count: Annotated[int, Meta(description="n"), FromBody]) -> str: ...
        provider = self._make_provider(handler)
        request = make_request(
            content_type="application/x-www-form-urlencoded",
            post_data={"count": "42"},
        )
        with pytest.raises(TypeError):
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
        assert args["item"].name == "widget"
        assert args["raw"] == body

    async def test_body_key_json(self) -> None:
        async def handler(name: Annotated[str, Meta(description="name"), BodyKey]) -> str: ...
        provider = self._make_provider(handler)
        request = make_request(
            content_type="application/json",
            body=msgspec.json.encode({"name": "widget"}),
        )
        args = await provider.gather_args(request)
        assert args["name"] == "widget"

    async def test_body_key_form(self) -> None:
        async def handler(count: Annotated[int, Meta(description="n"), BodyKey]) -> str: ...
        provider = self._make_provider(handler)
        request = make_request(
            content_type="application/x-www-form-urlencoded",
            post_data={"count": "42"},
        )
        args = await provider.gather_args(request)
        assert args["count"] == 42

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
        assert args["grant_type"] == "client_credentials"

    async def test_body_key_missing_required_raises(self) -> None:
        async def handler(name: Annotated[str, Meta(description="name"), BodyKey]) -> str: ...
        provider = self._make_provider(handler)
        request = make_request(content_type="application/json", body=msgspec.json.encode({}))
        with pytest.raises(TypeError):
            await provider.gather_args(request)

    async def test_body_key_optional_missing_returns_default(self) -> None:
        async def handler(
            name: Optional[Annotated[str, Meta(description="name"), BodyKey]] = "fallback",
        ) -> str: ...
        provider = self._make_provider(handler)
        request = make_request(content_type="application/json", body=msgspec.json.encode({}))
        args = await provider.gather_args(request)
        assert args["name"] == "fallback"

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
        assert args["item"].name == "widget"
        assert args["item"].count == 5
        assert args["name"] == "widget"

    async def test_param_constraint_enforced(self) -> None:
        async def handler(who: PathKey[str, Param("name", min_length=5)]) -> str: ...
        provider = self._make_provider(handler)
        request = make_request(match_info={"who": "abc"})
        with pytest.raises(TypeError):
            await provider.gather_args(request)

    async def test_param_constraint_passes(self) -> None:
        async def handler(who: PathKey[str, Param("name", min_length=5)]) -> str: ...
        provider = self._make_provider(handler)
        request = make_request(match_info={"who": "world!"})
        args = await provider.gather_args(request)
        assert args == {"who": "world!"}

    async def test_param_numeric_constraint(self) -> None:
        async def handler(age: QueryKey[int, Param("age", ge=18)]) -> str: ...
        provider = self._make_provider(handler)
        request = make_request(query={"age": "15"})
        with pytest.raises(TypeError):
            await provider.gather_args(request)

        request = make_request(query={"age": "21"})
        args = await provider.gather_args(request)
        assert args == {"age": 21}

    async def test_body_missing_required_raises(self) -> None:
        async def handler(name: Annotated[str, Meta(description="name"), FromBody]) -> str: ...
        provider = self._make_provider(handler)
        request = make_request(
            content_type="application/json",
            body=b"",
        )
        with pytest.raises(TypeError):
            await provider.gather_args(request)

    async def test_body_optional_empty_json_returns_default(self) -> None:
        async def handler(
            name: Optional[Annotated[str, Meta(description="name"), FromBody]] = None,
        ) -> str: ...
        provider = self._make_provider(handler)
        request = make_request(content_type="application/json", body=b"")
        args = await provider.gather_args(request)
        assert args["name"] is None

    async def test_body_optional_empty_json_returns_provided_default(self) -> None:
        async def handler(
            name: Optional[Annotated[str, Meta(description="name"), FromBody]] = "fallback",
        ) -> str: ...
        provider = self._make_provider(handler)
        request = make_request(content_type="application/json", body=b"")
        args = await provider.gather_args(request)
        assert args["name"] == "fallback"

    async def test_body_optional_empty_form_returns_default(self) -> None:
        class Item(msgspec.Struct):
            name: str

        async def handler(
            item: Optional[Annotated[Item, Meta(description="item"), FromBody]] = None,
        ) -> str: ...
        provider = self._make_provider(handler)
        request = make_request(content_type="application/x-www-form-urlencoded", post_data={})
        args = await provider.gather_args(request)
        assert args["item"] is None

    async def test_injected_request(self) -> None:
        async def handler(request: aioweb.Request) -> None: ...
        provider = self._make_provider(handler)
        mock_request = make_request()
        args = await provider.gather_args(mock_request)
        assert args["request"] is mock_request

    async def test_injected_server_hostname(self) -> None:
        async def handler(hostname: ServerHostname) -> None: ...
        provider = Provider(handler, Router()._type_injectors)
        request = make_request(headers={"Host": "example.com"})
        args = await provider.gather_args(request)
        assert args["hostname"] == "example.com"
        assert isinstance(args["hostname"], ServerHostname)

    async def test_injected_client_address(self) -> None:
        async def handler(client: ClientAddress) -> None: ...
        provider = Provider(handler, Router()._type_injectors)
        request = make_request(peername=("203.0.113.5", 54321))
        args = await provider.gather_args(request)
        assert args["client"] == "203.0.113.5:54321"
        assert isinstance(args["client"], ClientAddress)


# ---------------------------------------------------------------------------
# ServerHostname resolution
# ---------------------------------------------------------------------------

class TestServerHostname:

    def test_regular_hostname_is_returned_as_is(self) -> None:
        request = make_request(headers={"Host": "example.com"})
        assert get_server_hostname(request) == "example.com"

    def test_hostname_with_port_is_returned_as_is(self) -> None:
        request = make_request(headers={"Host": "example.com:8080"})
        assert get_server_hostname(request) == "example.com:8080"

    def test_ipv4_host_falls_back_to_fqdn(self) -> None:
        request = make_request(headers={"Host": "127.0.0.1"})
        assert get_server_hostname(request) == socket.getfqdn()

    def test_ipv6_host_falls_back_to_fqdn(self) -> None:
        request = make_request(headers={"Host": "::1"})
        assert get_server_hostname(request) == socket.getfqdn()

    def test_localhost_falls_back_to_fqdn(self) -> None:
        request = make_request(headers={"Host": "localhost"})
        assert get_server_hostname(request) == socket.getfqdn()

    def test_missing_host_header_falls_back_to_fqdn(self) -> None:
        request = make_request()
        assert get_server_hostname(request) == socket.getfqdn()

    def test_result_is_a_server_hostname(self) -> None:
        request = make_request(headers={"Host": "example.com"})
        assert isinstance(get_server_hostname(request), ServerHostname)


# ---------------------------------------------------------------------------
# ClientAddress resolution
# ---------------------------------------------------------------------------

class TestClientAddress:

    def test_direct_connection_uses_transport_peername(self) -> None:
        request = make_request(peername=("203.0.113.5", 54321))
        assert get_client_address(request) == "203.0.113.5:54321"

    def test_ipv6_peername_tuple_is_unpacked(self) -> None:
        request = make_request(peername=("::1", 9999, 0, 0))
        assert get_client_address(request) == "::1:9999"

    def test_forwarded_header_overrides_transport_peer_address(self) -> None:
        """Behind a reverse proxy, X-Forwarded-For is the real client, not the proxy."""
        request = make_request(
            headers={"X-Forwarded-For": "198.51.100.7"},
            peername=("127.0.0.1", 8080),
        )
        assert get_client_address(request) == "198.51.100.7:8080"

    def test_missing_port_falls_back_to_random_tag(self) -> None:
        request = make_request(headers={"X-Forwarded-For": "198.51.100.7"})
        addr, _, port = get_client_address(request).partition(":")
        assert addr == "198.51.100.7"
        assert len(port) == 4
        assert port.isalpha()

    def test_result_is_a_client_address(self) -> None:
        request = make_request(peername=("203.0.113.5", 54321))
        assert isinstance(get_client_address(request), ClientAddress)


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


class TestHttpRangeHelpers:

    def test_http_range_no_start_returns_none(self) -> None:
        assert http_range(slice(None, None)) is None

    def test_http_range_defaults_chunk_when_stop_missing(self) -> None:
        start, end = http_range(slice(0, None))
        assert start == 0
        assert end == 32767

    def test_http_range_explicit_bounds(self) -> None:
        assert http_range(slice(2, 5)) == (2, 5)

    def test_range_headers_within_bounds(self) -> None:
        headers = range_headers((2, 5), total_length=20)
        assert headers["Accept-Ranges"] == "bytes"
        assert headers["Content-Range"] == "bytes 2-5/20"

    def test_range_headers_clamps_end(self) -> None:
        headers = range_headers((10, 999), total_length=20)
        assert headers["Content-Range"] == "bytes 10-19/20"


class TestHttpRangeRequest:

    def _make_provider(self, handler) -> Provider:
        return Provider(handler, {HttpRangeRequest: HttpRangeRequest})

    async def test_no_range_header_returns_full_body(self) -> None:
        async def handler(range: HttpRangeRequest) -> aioweb.StreamResponse:
            return range.slice_response(b"0123456789ABCDEFGHIJ")

        provider = self._make_provider(handler)
        request = make_request()
        args = await provider.gather_args(request)
        resp = await handler(**args)
        assert resp.status == 200
        assert resp.body == b"0123456789ABCDEFGHIJ"

    async def test_range_header_returns_partial_bytes(self) -> None:
        async def handler(range: HttpRangeRequest) -> aioweb.StreamResponse:
            return range.slice_response(b"0123456789ABCDEFGHIJ")

        provider = self._make_provider(handler)
        request = make_request(headers={"Range": "bytes=2-5"})
        args = await provider.gather_args(request)
        resp = await handler(**args)
        assert resp.status == 206
        assert resp.body == b"2345"

    async def test_slice_merges_custom_headers(self) -> None:
        async def handler(range: HttpRangeRequest) -> aioweb.StreamResponse:
            range.response_headers["X-Call-Id"] = "abc"
            return range.slice_response(b"0123456789ABCDEFGHIJ")

        provider = self._make_provider(handler)
        request = make_request(headers={"Range": "bytes=2-5"})
        args = await provider.gather_args(request)
        resp = await handler(**args)
        assert resp.status == 206
        assert resp.headers["X-Call-Id"] == "abc"
        assert resp.headers["Content-Range"] == "bytes 2-5/20"

    async def test_bytes_range_exposes_start_and_end(self) -> None:
        async def handler(range: HttpRangeRequest) -> Optional[tuple[int, int]]:
            return range.bytes_range

        provider = self._make_provider(handler)
        request = make_request(headers={"Range": "bytes=2-5"})
        args = await provider.gather_args(request)
        assert await handler(**args) == (2, 5)

    async def test_bytes_range_is_none_without_range_header(self) -> None:
        async def handler(range: HttpRangeRequest) -> Optional[tuple[int, int]]:
            return range.bytes_range

        provider = self._make_provider(handler)
        request = make_request()
        args = await provider.gather_args(request)
        assert await handler(**args) is None

    async def test_chunk_wraps_pre_sliced_data(self) -> None:
        async def handler(range: HttpRangeRequest) -> aioweb.StreamResponse:
            data = "23456789"[:4]  # pretend a cache already sliced to bytes_range
            return range.chunk_response(data.encode(), total_length=20)

        provider = self._make_provider(handler)
        request = make_request(headers={"Range": "bytes=2-5"})
        args = await provider.gather_args(request)
        resp = await handler(**args)
        assert resp.status == 206
        assert resp.body == b"2345"
        assert resp.headers["Content-Range"] == "bytes 2-5/20"

    async def test_chunk_without_range_returns_full_body(self) -> None:
        async def handler(range: HttpRangeRequest) -> aioweb.StreamResponse:
            return range.chunk_response(b"0123456789ABCDEFGHIJ", total_length=20)

        provider = self._make_provider(handler)
        request = make_request()
        args = await provider.gather_args(request)
        resp = await handler(**args)
        assert resp.status == 200
        assert resp.body == b"0123456789ABCDEFGHIJ"

    async def test_chunk_unsatisfiable_returns_416(self) -> None:
        async def handler(range: HttpRangeRequest) -> aioweb.StreamResponse:
            return range.chunk_response(b"", total_length=20)

        provider = self._make_provider(handler)
        request = make_request(headers={"Range": "bytes=100-200"})
        args = await provider.gather_args(request)
        resp = await handler(**args)
        assert resp.status == 416
        assert resp.headers["Content-Range"] == "bytes */20"

    async def test_range_header_returns_partial_text(self) -> None:
        async def handler(range: HttpRangeRequest) -> aioweb.StreamResponse:
            return range.slice_response("0123456789ABCDEFGHIJ")

        provider = self._make_provider(handler)
        request = make_request(headers={"Range": "bytes=2-5"})
        args = await provider.gather_args(request)
        resp = await handler(**args)
        assert resp.status == 206
        assert resp.text == "2345"

    async def test_open_ended_range_clamps_to_content_length(self) -> None:
        async def handler(range: HttpRangeRequest) -> aioweb.StreamResponse:
            return range.slice_response(b"0123456789ABCDEFGHIJ")

        provider = self._make_provider(handler)
        request = make_request(headers={"Range": "bytes=15-"})
        args = await provider.gather_args(request)
        resp = await handler(**args)
        assert resp.status == 206
        assert resp.body == b"FGHIJ"
        assert resp.headers["Content-Range"] == "bytes 15-19/20"

    async def test_unsatisfiable_range_returns_416(self) -> None:
        async def handler(range: HttpRangeRequest) -> aioweb.StreamResponse:
            return range.slice_response(b"0123456789ABCDEFGHIJ")

        provider = self._make_provider(handler)
        request = make_request(headers={"Range": "bytes=100-200"})
        args = await provider.gather_args(request)
        resp = await handler(**args)
        assert resp.status == 416
        assert resp.headers["Content-Range"] == "bytes */20"

    async def test_malformed_range_header_serves_full_content(self) -> None:
        async def handler(range: HttpRangeRequest) -> aioweb.StreamResponse:
            return range.slice_response(b"0123456789")

        provider = self._make_provider(handler)
        request = make_request(headers={"Range": "not-a-range"})
        args = await provider.gather_args(request)
        resp = await handler(**args)
        assert resp.status == 200
        assert resp.body == b"0123456789"


def _written_body(request: aioweb.Request) -> bytes:
    """Collect the bytes a streamed response wrote to the (mocked) payload writer."""
    calls = request._payload_writer.write.call_args_list  # noqa: SLF001
    return b"".join(call.args[0] for call in calls)


class TestHttpRangeRequestStream:
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
        assert resp.status == 200
        assert _written_body(request) == b"0123456789ABCDEFGHIJ"

    async def test_range_header_streams_partial_content(self) -> None:
        async def handler(range: HttpRangeRequest) -> aioweb.StreamResponse:
            return await range.stream_response(FakeAsyncFile(b"0123456789ABCDEFGHIJ"))

        provider = self._make_provider(handler)
        request = make_mocked_request("GET", "/download", headers={"Range": "bytes=2-5"})
        args = await provider.gather_args(request)
        resp = await handler(**args)
        assert resp.status == 206
        assert _written_body(request) == b"2345"
        assert resp.headers["Content-Range"] == "bytes 2-5/20"

    async def test_stream_merges_custom_headers(self) -> None:
        async def handler(range: HttpRangeRequest) -> aioweb.StreamResponse:
            range.response_headers["X-Call-Id"] = "abc"
            return await range.stream_response(FakeAsyncFile(b"0123456789ABCDEFGHIJ"))

        provider = self._make_provider(handler)
        request = make_mocked_request("GET", "/download", headers={"Range": "bytes=2-5"})
        args = await provider.gather_args(request)
        resp = await handler(**args)
        assert resp.status == 206
        assert resp.headers["X-Call-Id"] == "abc"
        assert resp.headers["Content-Range"] == "bytes 2-5/20"

    async def test_streams_from_async_context_manager(self) -> None:
        async def handler(range: HttpRangeRequest) -> aioweb.StreamResponse:
            async with FakeAsyncOpen(b"0123456789ABCDEFGHIJ") as file:
                return await range.stream_response(file)

        provider = self._make_provider(handler)
        request = make_mocked_request("GET", "/download", headers={"Range": "bytes=5-9"})
        args = await provider.gather_args(request)
        resp = await handler(**args)
        assert resp.status == 206
        assert _written_body(request) == b"56789"

    async def test_unsatisfiable_range_returns_416_without_streaming(self) -> None:
        async def handler(range: HttpRangeRequest) -> aioweb.StreamResponse:
            return await range.stream_response(FakeAsyncFile(b"0123456789ABCDEFGHIJ"))

        provider = self._make_provider(handler)
        request = make_mocked_request("GET", "/download", headers={"Range": "bytes=100-200"})
        args = await provider.gather_args(request)
        resp = await handler(**args)
        assert resp.status == 416
        assert resp.headers["Content-Range"] == "bytes */20"

    async def test_reads_in_bounded_chunks(self) -> None:
        """A file larger than the stream chunk size is written in more than one chunk."""
        big = b"x" * (65536 + 10)

        async def handler(range: HttpRangeRequest) -> aioweb.StreamResponse:
            return await range.stream_response(FakeAsyncFile(big))

        provider = self._make_provider(handler)
        request = make_mocked_request("GET", "/download")
        args = await provider.gather_args(request)
        resp = await handler(**args)
        assert resp.status == 200
        calls = request._payload_writer.write.call_args_list  # noqa: SLF001
        assert len(calls) > 1
        assert _written_body(request) == big

    async def test_aiofile_style_wrapper_with_explicit_total_length(self) -> None:
        """aiofile's real seek() is sync, single-arg, and returns None — must still work."""
        async def handler(range: HttpRangeRequest) -> aioweb.StreamResponse:
            return await range.stream_response(FakeAiofileWrapper(b"0123456789ABCDEFGHIJ"), total_length=20)

        provider = self._make_provider(handler)
        request = make_mocked_request("GET", "/download", headers={"Range": "bytes=2-5"})
        args = await provider.gather_args(request)
        resp = await handler(**args)
        assert resp.status == 206
        assert _written_body(request) == b"2345"
        assert resp.headers["Content-Range"] == "bytes 2-5/20"

    async def test_aiofile_style_wrapper_full_download_with_explicit_total_length(self) -> None:
        async def handler(range: HttpRangeRequest) -> aioweb.StreamResponse:
            return await range.stream_response(FakeAiofileWrapper(b"0123456789ABCDEFGHIJ"), total_length=20)

        provider = self._make_provider(handler)
        request = make_mocked_request("GET", "/download")
        args = await provider.gather_args(request)
        resp = await handler(**args)
        assert resp.status == 200
        assert _written_body(request) == b"0123456789ABCDEFGHIJ"

    async def test_missing_total_length_raises_clear_error_for_unsupported_seek(self) -> None:
        async def handler(range: HttpRangeRequest) -> aioweb.StreamResponse:
            return await range.stream_response(FakeAiofileWrapper(b"hello"))

        provider = self._make_provider(handler)
        request = make_mocked_request("GET", "/download")
        args = await provider.gather_args(request)
        with pytest.raises(TypeError):
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


class TestRouter:

    @pytest.fixture(autouse=True)
    async def _client(self, aiohttp_client) -> None:
        self.client = await aiohttp_client(build_app())

    async def test_path_param(self) -> None:
        resp = await self.client.get("/hello/world")
        assert resp.status == 200
        assert await resp.json() == "Hello, world"
        assert resp.content_type == "application/json"

    async def test_query_param(self) -> None:
        resp = await self.client.get("/greet?who=world")
        assert resp.status == 200
        assert await resp.json() == "Hello, world"

    async def test_missing_required_param_returns_400(self) -> None:
        resp = await self.client.get("/greet")
        assert resp.status == 400

    async def test_optional_param_uses_default(self) -> None:
        resp = await self.client.get("/maybe")
        assert resp.status == 200
        assert await resp.json() == "Hello, Nobody"

    async def test_optional_param_provided(self) -> None:
        resp = await self.client.get("/maybe?who=Kenny")
        assert resp.status == 200
        assert await resp.json() == "Hello, Kenny"

    async def test_plain_route_without_meta(self) -> None:
        resp = await self.client.get("/plain/world")
        assert resp.status == 200
        assert await resp.text() == "Hi, world"

    async def test_subscript_route(self) -> None:
        resp = await self.client.get("/short/world")
        assert resp.status == 200
        assert await resp.json() == "Hey, world"

    async def test_redirect(self) -> None:
        resp = await self.client.get("/old", allow_redirects=False)
        assert resp.status == 302
        assert resp.headers["Location"] == "/hello/world"

    async def test_redirect_follows(self) -> None:
        resp = await self.client.get("/old")
        assert resp.status == 200
        assert await resp.json() == "Hello, world"

    async def test_http_error(self) -> None:
        resp = await self.client.get("/gone")
        assert resp.status == 410
        assert await resp.text() == "This resource is gone"

    async def test_download_without_range_returns_full_body(self) -> None:
        resp = await self.client.get("/download")
        assert resp.status == 200
        assert await resp.read() == b"0123456789ABCDEFGHIJ"

    async def test_download_with_range_returns_partial_content(self) -> None:
        resp = await self.client.get("/download", headers={"Range": "bytes=2-5"})
        assert resp.status == 206
        assert await resp.read() == b"2345"
        assert resp.headers["Content-Range"] == "bytes 2-5/20"

    async def test_download_stream_without_range_returns_full_body(self) -> None:
        resp = await self.client.get("/download-stream")
        assert resp.status == 200
        assert await resp.read() == b"0123456789ABCDEFGHIJ"

    async def test_download_stream_with_range_returns_partial_content(self) -> None:
        resp = await self.client.get("/download-stream", headers={"Range": "bytes=2-5"})
        assert resp.status == 206
        assert await resp.read() == b"2345"
        assert resp.headers["Content-Range"] == "bytes 2-5/20"


class _NotFoundBody(msgspec.Struct):
    detail: str


class _NotFoundError(ApiError):
    """Resource not found."""

    status_code = 404
    response_type = _NotFoundBody

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(detail)

    def to_response(self) -> _NotFoundBody:
        return _NotFoundBody(detail=self.detail)


def build_app_with_output_handlers() -> aioweb.Application:
    """Create an app whose ApiRouter wraps successes/errors in an envelope."""
    router = Router(
        on_result=lambda method, path, value: {"method": method, "path": path, "data": value},
        on_error=lambda method, path, error: {"method": method, "path": path, "error": str(error)},
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

    @router.api.get("/not-found")
    async def not_found() -> None:
        raise _NotFoundError("User 42 not found")  # noqa: TRY003

    app = aioweb.Application()
    app.add_routes(router)
    return app


class TestApiRouterOutputHandlers:

    @pytest.fixture(autouse=True)
    async def _client(self, aiohttp_client) -> None:
        self.client = await aiohttp_client(build_app_with_output_handlers())

    async def test_on_result_wraps_successful_response(self) -> None:
        resp = await self.client.get("/greet?who=world")
        assert resp.status == 200
        assert await resp.json() == {"method": "GET", "path": "/greet", "data": "Hello, world"}

    async def test_on_error_wraps_unhandled_exception(self) -> None:
        resp = await self.client.get("/boom")
        assert resp.status == 500
        assert await resp.json() == {"method": "GET", "path": "/boom", "error": "kaboom"}

    async def test_deliberate_http_exception_bypasses_on_error(self) -> None:
        resp = await self.client.get("/gone")
        assert resp.status == 410
        assert await resp.text() == "This resource is gone"

    async def test_api_error_bypasses_on_error_and_renders_its_own_status(self) -> None:
        """An ApiError raised by a handler renders itself even when on_error is configured."""
        resp = await self.client.get("/not-found")
        assert resp.status == 404
        assert await resp.json() == {"detail": "User 42 not found"}


def build_app_with_late_bound_output_handler() -> aioweb.Application:
    """Set router.api.on_result after the route is decorated but before build() runs."""
    router = Router()

    @router.api.get("/greet")
    async def greet(who: Annotated[str, Meta(description="name")]) -> str:
        return f"Hello, {who}"

    router.api.on_result = lambda method, path, value: {"method": method, "path": path, "data": value}

    app = aioweb.Application()
    app.add_routes(router)
    return app


class TestApiRouterOutputHandlerSetAfterDecoration:

    @pytest.fixture(autouse=True)
    async def _client(self, aiohttp_client) -> None:
        self.client = await aiohttp_client(build_app_with_late_bound_output_handler())

    async def test_on_result_set_after_decoration_still_applies(self) -> None:
        resp = await self.client.get("/greet?who=world")
        assert resp.status == 200
        assert await resp.json() == {"method": "GET", "path": "/greet", "data": "Hello, world"}


def build_app_with_builtin_error_responses() -> aioweb.Application:
    """Create an app relying on the router's built-in structured 400/500 (no on_error/on_result)."""
    router = Router()

    @router.api.get("/greet")
    async def greet(who: Annotated[str, Meta(description="name"), QueryKey]) -> str:
        return f"Hello, {who}"

    @router.api.get("/boom")
    async def boom() -> str:
        msg = "kaboom"
        raise ValueError(msg)

    @router.get("/plain/greet")
    async def plain_greet(who: Annotated[str, Meta(description="name"), QueryKey]) -> str:
        return f"Hi, {who}"

    @router.get("/plain/boom")
    async def plain_boom() -> str:
        msg = "kaboom"
        raise ValueError(msg)

    class BrokenService:
        pass

    def broken_injector(request: aioweb.Request) -> BrokenService:  # noqa: ARG001
        msg = "injector exploded"
        raise RuntimeError(msg)

    router.add_type_injector(BrokenService, broken_injector)

    @router.api.get("/broken-injector")
    async def broken_injector_route(svc: BrokenService) -> str:  # noqa: ARG001
        return "unreachable"

    app = aioweb.Application()
    app.add_routes(router)
    return app


class TestBuiltinErrorResponses:

    @pytest.fixture(autouse=True)
    async def _client(self, aiohttp_client) -> None:
        self.client = await aiohttp_client(build_app_with_builtin_error_responses())

    async def test_api_route_validation_error_is_structured_json(self) -> None:
        resp = await self.client.get("/greet")
        assert resp.status == 400
        body = await resp.json()
        assert len(body["errors"]) == 1
        assert body["errors"][0]["attribute"] == "who"
        assert body["errors"][0]["source"] == "query"
        assert body["errors"][0]["error"] == "Missing required value"

    async def test_api_route_unhandled_exception_is_structured_json(self) -> None:
        """The original exception message ("kaboom") must never reach the client."""
        resp = await self.client.get("/boom")
        assert resp.status == 500
        assert await resp.json() == {"error": "Internal Server Error"}

    async def test_plain_route_validation_error_stays_plain_text(self) -> None:
        resp = await self.client.get("/plain/greet")
        assert resp.status == 400
        assert resp.content_type != "application/json"

    async def test_plain_route_unhandled_exception_is_not_structured_json(self) -> None:
        resp = await self.client.get("/plain/boom")
        assert resp.status == 500
        assert resp.content_type != "application/json"

    async def test_non_argument_error_during_gather_args_is_structured_json(self) -> None:
        """A non-ArgumentError exception while gathering args (e.g. a buggy injector) still

        gets the structured 500 treatment, not a raw crash.
        """
        resp = await self.client.get("/broken-injector")
        assert resp.status == 500
        assert await resp.json() == {"error": "Internal Server Error"}


class TestDuplicateRoute:

    def test_duplicate_api_route_raises(self) -> None:
        router = Router()

        @router.api.get("/items")
        async def list_items(request: aioweb.Request) -> None: ...

        @router.api.get("/items")
        async def list_items_again(request: aioweb.Request) -> None: ...

        with pytest.raises(ValueError, match="Duplicate route: GET /items"):
            router.build()

    def test_duplicate_plain_route_raises(self) -> None:
        router = Router()

        @router.get("/page")
        async def page(request: aioweb.Request) -> None: ...

        @router.get("/page")
        async def page_again(request: aioweb.Request) -> None: ...

        with pytest.raises(ValueError, match="Duplicate route: GET /page"):
            router.build()

    def test_duplicate_across_api_and_plain_raises(self) -> None:
        router = Router()

        @router.api.get("/shared")
        async def api_handler(request: aioweb.Request) -> None: ...

        @router.get("/shared")
        async def plain_handler(request: aioweb.Request) -> None: ...

        with pytest.raises(ValueError, match="Duplicate route: GET /shared"):
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

        with pytest.raises(ValueError, match="Duplicate route: GET /hello"):
            router.build()


# ---------------------------------------------------------------------------
# Pending route collection tests
# ---------------------------------------------------------------------------

class TestPendingRoutes:

    def test_api_get_stores_metadata(self) -> None:
        router = Router()

        @router.api.get('/items/{id}', tag='Items')
        async def get_item() -> None: ...

        assert len(router._pending) == 1
        entry = router._pending[0]
        assert entry.method == 'GET'
        assert entry.path == '/items/{id}'
        assert entry.router_type == 'api'
        assert entry.tag == 'Items'
        assert entry.handler is get_item

    def test_plain_post_stores_metadata(self) -> None:
        router = Router()

        @router.post('/submit')
        async def submit() -> None: ...

        assert len(router._pending) == 1
        entry = router._pending[0]
        assert entry.method == 'POST'
        assert entry.router_type == 'plain'

    def test_multiple_decorators_on_same_handler(self) -> None:
        router = Router()

        @router.api.get('/items')
        @router.api.get('/all-items')
        async def list_items() -> None: ...

        assert len(router._pending) == 2

    def test_api_get_forwards_unrecognized_kwargs(self) -> None:
        """Unknown kwargs (e.g. aiohttp's own name=) are stored, not silently dropped."""
        router = Router()

        @router.api.get('/items/{id}', name='get_item')
        async def get_item() -> None: ...

        assert router._pending[0].kwargs == {'name': 'get_item'}

    def test_plain_get_forwards_unrecognized_kwargs(self) -> None:
        router = Router()

        @router.get('/items/{id}', name='get_item')
        async def get_item() -> None: ...

        assert router._pending[0].kwargs == {'name': 'get_item'}

    def test_forwarded_kwargs_reach_aiohttps_own_route_registration(self) -> None:
        """name= isn't just stored — it actually reaches aiohttp's UrlDispatcher."""
        router = Router()

        @router.api.get('/items/{id}', name='get_item')
        async def get_item(id: PathKey[str, 'id']) -> str:
            return id

        app = aioweb.Application()
        app.add_routes(router)
        assert app.router['get_item'].url_for(id='42') == URL('/items/42')


# ---------------------------------------------------------------------------
# Unbound self detection
# ---------------------------------------------------------------------------

class TestUnboundSelfDetection:

    def test_missing_include_raises(self) -> None:
        router = Router()

        class Greeter:
            @router.api.get('/greet')
            async def hello(self) -> str:
                return 'hi'

        with pytest.raises(TypeError, match='unbound "self"'):
            router.build()


class TestIncludeModule:
    """router.include() also accepts a module: a real use of the import, plus auto-tagging."""

    def test_include_module_is_a_noop_for_unrelated_module(self) -> None:
        router = Router()

        @router.api.get("/greet")
        async def greet() -> str:
            return "hi"

        routes_module = types.ModuleType("fake_routes_module")
        routes_module.greet = greet  # type: ignore[attr-defined]

        pending_before = list(router._pending)
        router.include(routes_module)
        assert router._pending == pending_before

    def test_include_module_does_not_raise(self) -> None:
        router = Router()

        @router.get("/plain")
        async def plain() -> str:
            return "hi"

        router.include(sys.modules[__name__])
        assert len(router._pending) == 1

    def test_include_module_tags_untagged_api_routes_with_last_name_segment(self) -> None:
        router = Router()

        @router.api.get("/greet")
        async def greet() -> str:
            return "hi"

        fake_module = types.ModuleType(__name__)
        router.include(fake_module)
        assert router._pending[0].tag == __name__.rsplit(".", 1)[-1]

    def test_include_module_dunder_tag_overrides_default(self) -> None:
        router = Router()

        @router.api.get("/greet")
        async def greet() -> str:
            return "hi"

        fake_module = types.ModuleType(__name__)
        fake_module.__tag__ = "CustomTag"  # type: ignore[attr-defined]
        router.include(fake_module)
        assert router._pending[0].tag == "CustomTag"

    def test_include_module_does_not_override_explicit_tag(self) -> None:
        router = Router()

        @router.api.get("/greet", tag="Explicit")
        async def greet() -> str:
            return "hi"

        fake_module = types.ModuleType(__name__)
        router.include(fake_module)
        assert router._pending[0].tag == "Explicit"

    def test_include_module_does_not_tag_plain_routes(self) -> None:
        router = Router()

        @router.get("/plain")
        async def plain() -> str:
            return "hi"

        fake_module = types.ModuleType(__name__)
        router.include(fake_module)
        assert router._pending[0].tag is None

    def test_include_module_dunder_responses_applied_when_unset(self) -> None:
        router = Router()

        @router.api.get("/greet")
        async def greet() -> str:
            return "hi"

        class NotFoundError(msgspec.Struct):
            detail: str

        fake_module = types.ModuleType(__name__)
        fake_module.__responses__ = {404: NotFoundError}  # type: ignore[attr-defined]
        router.include(fake_module)
        assert router._pending[0].responses == {404: NotFoundError}

    def test_include_module_dunder_responses_merges_with_explicit_responses(self) -> None:
        router = Router()

        class NotFoundError(msgspec.Struct):
            detail: str

        class ConflictError(msgspec.Struct):
            detail: str

        @router.api.get("/greet", responses={409: ConflictError})
        async def greet() -> str:
            return "hi"

        fake_module = types.ModuleType(__name__)
        fake_module.__responses__ = {404: NotFoundError, 409: NotFoundError}  # type: ignore[attr-defined]
        router.include(fake_module)
        assert router._pending[0].responses == {404: NotFoundError, 409: ConflictError}

    def test_include_module_does_not_apply_responses_to_plain_routes(self) -> None:
        router = Router()

        @router.get("/plain")
        async def plain() -> str:
            return "hi"

        class NotFoundError(msgspec.Struct):
            detail: str

        fake_module = types.ModuleType(__name__)
        fake_module.__responses__ = {404: NotFoundError}  # type: ignore[attr-defined]
        router.include(fake_module)
        assert router._pending[0].responses is None


# ---------------------------------------------------------------------------
# Router.include() integration tests (stateful — real aiohttp test server)
# ---------------------------------------------------------------------------

def build_app_with_include() -> aioweb.Application:
    """Create an app whose routes are registered via router.include()."""
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


class TestRouterInclude:

    @pytest.fixture(autouse=True)
    async def _client(self, aiohttp_client) -> None:
        self.client = await aiohttp_client(build_app_with_include())

    async def test_include_path_param(self) -> None:
        resp = await self.client.get('/greet/world')
        assert resp.status == 200
        assert await resp.json() == 'Hello, world'

    async def test_include_prefix_only(self) -> None:
        resp = await self.client.get('/greet')
        assert resp.status == 200
        assert await resp.json() == 'everyone'

    async def test_include_html_route(self) -> None:
        resp = await self.client.get('/greet/page')
        assert resp.status == 200
        assert resp.content_type == 'text/html'
        assert await resp.text() == '<h1>Hi</h1>'

    async def test_include_absolute_path(self) -> None:
        resp = await self.client.get('/absolute/path')
        assert resp.status == 200
        assert await resp.json() == 'absolute'

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

        from aiohttp.test_utils import TestClient, TestServer
        server = TestServer(app)
        client = TestClient(server)
        await client.start_server()
        try:
            resp = await client.get('/injected')
            assert resp.status == 200
            assert await resp.json() == '42'
        finally:
            await client.close()


# ---------------------------------------------------------------------------
# OpenAPIBuilder parameter/requestBody classification
# ---------------------------------------------------------------------------

class TestOpenAPIBuilderInfo:

    def test_description_is_omitted_by_default(self) -> None:
        spec = OpenAPIBuilder().build()
        assert "description" not in spec["info"]

    def test_description_is_included_when_set(self) -> None:
        builder = OpenAPIBuilder()
        builder.description = "Responses are wrapped in {success, data}."
        spec = builder.build()
        assert spec["info"]["description"] == "Responses are wrapped in {success, data}."


class TestOpenAPIBuilderParameters:

    def _operation(self, handler) -> dict:
        builder = OpenAPIBuilder()
        builder.add_route("GET", "/x/{who}", handler, {})
        return builder._paths["/x/{who}"]["get"]

    def test_plain_path_param(self) -> None:
        async def handler(who: Annotated[str, Meta(description="name")]) -> str: ...
        op = self._operation(handler)
        assert op["parameters"] == [{
            "name": "who", "in": "path", "required": True, "schema": {"type": "string"},
            "description": "name",
        }]

    def test_plain_query_param(self) -> None:
        async def handler(who: Annotated[str, Meta(description="name")], age: Annotated[int, Meta(description="n")]) -> str: ...
        op = self._operation(handler)
        age_param = next(p for p in op["parameters"] if p["name"] == "age")
        assert age_param["in"] == "query"

    def test_path_key_is_in_path(self) -> None:
        async def handler(who: PathKey[str, "someone"]) -> str: ...
        op = self._operation(handler)
        assert op["parameters"][0]["in"] == "path"

    def test_query_key_is_in_query(self) -> None:
        async def handler(who: QueryKey[str, "someone"]) -> str: ...
        op = self._operation(handler)
        assert op["parameters"][0]["in"] == "query"

    def test_header_key_is_in_header(self) -> None:
        async def handler(token: HeaderKey[str, "auth"]) -> str: ...
        op = self._operation(handler)
        assert op["parameters"][0]["in"] == "header"

    def test_cookie_key_is_in_cookie(self) -> None:
        async def handler(session: CookieKey[str, "session"]) -> str: ...
        op = self._operation(handler)
        assert op["parameters"][0]["in"] == "cookie"

    def test_from_body_struct_becomes_request_body_not_query_param(self) -> None:
        class UserSettings(msgspec.Struct):
            active: bool

        async def handler(user: FromBody[UserSettings, Param("User settings")]) -> int: ...
        op = self._operation(handler)
        assert op["parameters"] == []
        assert "requestBody" in op
        schema = op["requestBody"]["content"]["application/json"]["schema"]
        assert schema == {"$ref": "#/components/schemas/UserSettings"}

    def test_from_body_struct_docstring_is_dedented_in_schema(self) -> None:
        class UserSettings(msgspec.Struct):
            """Fields accepted when creating or updating a user

            Fields left unset are omitted from the request, so on PATCH they
            leave the existing value untouched.
            """

            active: bool

        async def handler(user: FromBody[UserSettings, Param("User settings")]) -> int: ...
        builder = OpenAPIBuilder()
        builder.add_route("GET", "/x", handler, {})
        description = builder._schemas["UserSettings"]["description"]
        for line in description.splitlines():
            assert not line.startswith(" "), f"line has leftover indentation: {line!r}"
        assert "Fields accepted when creating or updating a user" in description

    def test_body_key_becomes_request_body_property(self) -> None:
        async def handler(
            grant_type: Annotated[str, Meta(description="grant type"), BodyKey],
        ) -> str: ...
        op = self._operation(handler)
        assert op["parameters"] == []
        schema = op["requestBody"]["content"]["application/json"]["schema"]
        assert schema["type"] == "object"
        assert schema["properties"]["grant_type"]["description"] == "grant type"
        assert "grant_type" in schema["required"]

    def test_from_query_struct_expands_into_query_params(self) -> None:
        class Paging(msgspec.Struct):
            limit: int
            offset: int

        async def handler(paging: FromQuery[Paging, "pagination"]) -> str: ...
        op = self._operation(handler)
        names = {p["name"]: p["in"] for p in op["parameters"]}
        assert names == {"limit": "query", "offset": "query"}

    def test_from_path_struct_expands_into_path_params(self) -> None:
        class Segments(msgspec.Struct):
            user_id: int

        async def handler(segments: FromPath[Segments, "segments"]) -> str: ...
        op = self._operation(handler)
        assert op["parameters"] == [{
            "name": "user_id", "in": "path", "required": True, "schema": {"type": "integer"},
        }]

    def test_request_body_gets_a_synthesized_example(self) -> None:
        class PhoneSelector(msgspec.Struct, kw_only=True):
            identifier: Optional[Annotated[str, Meta(description="Device identificator", examples=["1001"])]] = None
            extension: Optional[str] = None

        async def handler(sel: FromBody[PhoneSelector, "selector"]) -> None: ...
        op = self._operation(handler)
        example = op["requestBody"]["content"]["application/json"]["example"]
        assert example["identifier"] == "1001"
        assert example["extension"] is not None

    def test_response_gets_a_synthesized_example(self) -> None:
        class Item(msgspec.Struct, kw_only=True):
            name: Annotated[str, Meta(examples=["widget"])]
            note: Optional[str] = None

        async def handler() -> Item: ...
        op = self._operation(handler)
        example = op["responses"]["200"]["content"]["application/json"]["example"]
        assert example["name"] == "widget"
        assert example["note"] is not None


class TestOpenAPIBuilderResponses:

    def _operation(self, handler, responses=None) -> dict:
        builder = OpenAPIBuilder()
        builder.add_route("GET", "/x/{who}", handler, {}, responses=responses)
        return builder._paths["/x/{who}"]["get"]

    def test_builtin_400_and_500_are_always_documented(self) -> None:
        async def handler(who: PathKey[str, "someone"]) -> str: ...
        op = self._operation(handler)
        assert op["responses"]["400"]["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/ValidationErrorResponse",
        }
        assert op["responses"]["500"]["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/ServerErrorResponse",
        }

    def test_builtin_400_example_reports_attribute_source_and_error(self) -> None:
        async def handler(who: PathKey[str, "someone"]) -> str: ...
        op = self._operation(handler)
        example = op["responses"]["400"]["content"]["application/json"]["example"]
        assert set(example["errors"][0]) == {"attribute", "source", "error"}

    def test_custom_responses_are_added(self) -> None:
        class NotFoundError(msgspec.Struct):
            """Resource not found."""

            detail: str

        async def handler(who: PathKey[str, "someone"]) -> str: ...
        op = self._operation(handler, responses={404: NotFoundError})
        assert op["responses"]["404"]["description"] == "Resource not found."
        assert op["responses"]["404"]["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/NotFoundError",
        }

    def test_custom_responses_can_override_builtin_400(self) -> None:
        class CustomBadRequest(msgspec.Struct):
            """Custom bad request."""

            message: str

        async def handler(who: PathKey[str, "someone"]) -> str: ...
        op = self._operation(handler, responses={400: CustomBadRequest})
        assert op["responses"]["400"]["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/CustomBadRequest",
        }

    def test_same_named_different_structs_raise_on_collision(self) -> None:
        """Two unrelated types sharing a class name must not silently clobber each other's schema."""
        builder = OpenAPIBuilder()

        first = type("Item", (msgspec.Struct,), {"__annotations__": {"sku": str}})
        second = type("Item", (msgspec.Struct,), {"__annotations__": {"title": str}})

        async def handler_a(item: FromBody[first, "item"]) -> None: ...  # type: ignore[valid-type]
        async def handler_b(item: FromBody[second, "item"]) -> None: ...  # type: ignore[valid-type]

        builder.add_route("POST", "/a", handler_a, {})
        with pytest.raises(ValueError, match="schema name collision"):
            builder.add_route("POST", "/b", handler_b, {})

    def test_same_struct_registered_twice_does_not_raise(self) -> None:
        """Registering the *same* type from two different routes is fine (the common case)."""
        builder = OpenAPIBuilder()

        class Item(msgspec.Struct):
            sku: str

        async def handler_a(item: FromBody[Item, "item"]) -> None: ...
        async def handler_b(item: FromBody[Item, "item"]) -> None: ...

        builder.add_route("POST", "/a", handler_a, {})
        builder.add_route("POST", "/b", handler_b, {})  # should not raise


class TestExampleFor:
    """OpenAPIBuilder._example_for() digs into anyOf branches instead of showing null."""

    def test_plain_examples_used_directly(self) -> None:
        builder = OpenAPIBuilder()
        assert builder._example_for({"type": "string", "examples": ["a"]}) == "a"

    def test_optional_field_uses_non_null_branch_examples(self) -> None:
        builder = OpenAPIBuilder()
        schema = {
            "anyOf": [
                {"type": "string", "description": "Device identificator", "examples": ["1001"]},
                {"type": "null"},
            ],
            "default": None,
        }
        assert builder._example_for(schema) == "1001"

    def test_optional_field_without_examples_falls_back_to_type_default(self) -> None:
        builder = OpenAPIBuilder()
        schema = {"anyOf": [{"type": "integer"}, {"type": "null"}], "default": None}
        assert builder._example_for(schema) == 0

    def test_object_with_optional_fields_none_are_null(self) -> None:
        builder = OpenAPIBuilder()
        schema = {
            "type": "object",
            "properties": {
                "identifier": {
                    "anyOf": [
                        {"type": "string", "examples": ["1001"]},
                        {"type": "null"},
                    ],
                },
                "extension": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            },
        }
        example = builder._example_for(schema)
        assert example["identifier"] == "1001"
        assert example["extension"] is not None
