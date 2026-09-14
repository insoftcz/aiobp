"""Argument validation and dependency injection for aiohttp"""

# We have files prefixed with underscore because pyright in Zed was
# too eager and hinter saw exported classes and methods twice. For example
# router was visible via aiobp.aiohttp and aiobp.aiohttp.web. Nothing of
# following hepled:
# - having py.typed
# - having _aiohttp folder
# - importing with aliases (from web import router as router)

from aiobp.aiohttp._connection import ClientAddress, ServerHostname
from aiobp.aiohttp._http_range import HttpRangeRequest, http_range, range_headers
from aiobp.aiohttp._provider import (
    ApiError,
    BodyKey,
    CookieKey,
    FromBody,
    FromPath,
    FromQuery,
    HeaderKey,
    Param,
    PathKey,
    QueryKey,
    ServerError,
)
from aiobp.aiohttp._router import ApiRouter, BuiltinRouter, Router, router
from aiobp.aiohttp._server import WebServer

__all__ = [
    "ApiError",
    "ApiRouter",
    "BodyKey",
    "BuiltinRouter",
    "ClientAddress",
    "CookieKey",
    "FromBody",
    "FromPath",
    "FromQuery",
    "HeaderKey",
    "HttpRangeRequest",
    "Param",
    "PathKey",
    "QueryKey",
    "Router",
    "ServerError",
    "ServerHostname",
    "WebServer",
    "http_range",
    "range_headers",
    "router",
]
