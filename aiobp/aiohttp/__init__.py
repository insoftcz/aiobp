"""Argument validation and dependency injection for aiohttp"""

from .server import WebServer
from .web import Router, router

__all__ = [
    "Router",
    "WebServer",
    "router",
]
