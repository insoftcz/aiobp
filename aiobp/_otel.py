"""Shared helpers for the optional OpenTelemetry integration."""

import socket
from typing import Optional
from urllib.parse import urlparse

_DEFAULT_OTLP_GRPC_PORT = 4317


def endpoint_reachable(endpoint: str, timeout: float = 1.0) -> bool:
    """Quick TCP probe so callers can disable telemetry instead of blocking on a down collector.

    OTLP gRPC exporters connect lazily and retry with backoff; with a synchronous
    processor every emit blocks on that loop, stalling startup. An unparseable
    endpoint returns True so telemetry is still attempted rather than dropped.
    """
    host, port = _split_host_port(endpoint)
    if host is None:
        return True
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _split_host_port(endpoint: str) -> tuple[Optional[str], int]:
    """Parse (host, port) from an OTLP endpoint, with or without a scheme."""
    raw = endpoint.strip()
    if "://" not in raw:
        raw = "//" + raw  # force urlparse to treat a bare host:port as netloc
    try:
        parsed = urlparse(raw)
        host = parsed.hostname
        port = parsed.port or _DEFAULT_OTLP_GRPC_PORT  # .port raises ValueError on a non-numeric port
    except ValueError:
        return None, _DEFAULT_OTLP_GRPC_PORT
    return host, port
