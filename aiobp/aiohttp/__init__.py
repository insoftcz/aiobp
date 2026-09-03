"""Argument validation and dependency injection for aiohttp"""

# We have files prefixed with underscore because pyright in Zed was
# too eager and hinter saw exported classes and methods twice. For example
# router was visible via aiobp.aiohttp and aiobp.aiohttp.web. Nothing of
# following hepled:
# - having py.typed
# - having _aiohttp folder
# - importing with aliases (from web import router as router)

from ._connection import ClientAddress, ServerHostname
from ._http_range import HttpRangeRequest, http_range, range_headers
from ._provider import BodyKey, CookieKey, FromBody, FromPath, FromQuery, HeaderKey, Param, PathKey, QueryKey
from ._router import ApiRouter, Router, router
from ._server import WebServer

__all__ = [
    "ApiRouter",
    "BodyKey",
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
    "ServerHostname",
    "WebServer",
    "http_range",
    "range_headers",
    "router",
]
