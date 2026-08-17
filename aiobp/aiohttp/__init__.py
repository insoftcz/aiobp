"""Argument validation and dependency injection for aiohttp"""

from .server import WebServer
from .web import HtmlRouter, RestRouter, Router, router

__all__ = [
    "HtmlRouter",
    "RestRouter",
    "Router",
    "WebServer",
    "router",
]
