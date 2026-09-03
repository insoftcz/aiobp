"""Server hostname and client address resolution, injected via type annotation."""

import ipaddress
import random
import socket
import string

from aiohttp import hdrs, web


class ServerHostname(str):
    """The server's own hostname, injected from the request's ``Host`` header.

    Register as a type injector so handlers can request it directly::

        async def whoami(hostname: ServerHostname) -> str:
            return f"Serving from {hostname}"

    Falls back to the local machine's FQDN when the ``Host`` header is
    missing, an IP address, or ``localhost`` — none of those are a
    meaningful hostname to report back to a caller. To override this
    behavior entirely (e.g. a fixed public hostname behind a proxy), register
    a replacement factory with ``router.add_type_injector(ServerHostname, ...)``.
    """

    __slots__ = ()


def get_server_hostname(request: web.Request) -> ServerHostname:
    """Resolve the server's hostname for the current request."""
    host = request.headers.get(hdrs.HOST)
    if host is not None and host != "localhost":
        try:
            ipaddress.ip_address(host)
        except ValueError:
            # Not an IP literal, so it's a real hostname (e.g. from a reverse proxy).
            return ServerHostname(host)

    return ServerHostname(socket.getfqdn())


class ClientAddress(str):
    """The remote client's ``address:port``, injected from the connection/request.

    Register as a type injector so handlers can request it directly::

        async def whoami(client: ClientAddress) -> str:
            return f"Request from {client}"

    Prefers the ``X-Forwarded-For`` header over the raw transport peer address,
    since behind a reverse proxy (Apache, Nginx, ...) the transport only sees
    the proxy's address. The port is a debugging aid, not an authoritative
    value: it falls back to a random 4-letter tag when the real one isn't
    known (e.g. it can't be recovered from ``X-Forwarded-For`` alone).
    """

    __slots__ = ()


def get_client_address(request: web.Request) -> ClientAddress:
    """Resolve the remote client's address for the current request."""
    transport = request.transport
    peer_host, peer_port, *_ = transport.get_extra_info("peername") if transport else (None, None)

    # Behind a reverse proxy (Apache, Nginx, ...) the transport peer is the
    # proxy itself, so prefer the client address from the forwarding header.
    addr = request.headers.get(hdrs.X_FORWARDED_FOR, peer_host)
    port = peer_port or "".join(random.choices(string.ascii_lowercase, k=4))  # noqa: S311 - just to identify connection
    return ClientAddress(f"{addr}:{port}")
