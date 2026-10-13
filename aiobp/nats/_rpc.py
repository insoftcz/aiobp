"""RPC wire format: request `[args, kwargs]`, reply `[1, result]` or `[code, message]`"""

import reprlib
from collections.abc import Awaitable, Callable, Sequence
from typing import TYPE_CHECKING, Any

from aiobp.nats._common import INVALID, decode_payload

if TYPE_CHECKING:
    from aiobp.nats._nats import Nats

RpcHandler = Callable[..., Any]

RPC_PREFIX = "rpc."
RPC_OK = 1

_repr = reprlib.Repr()
_repr.maxstring = 100
_repr.maxother = 100


class RpcError(Exception):
    """RPC call failed on the remote side

    Raise it from an RPC handler to reply with an error `message` (and optional
    `code`) without logging a traceback — use it for expected failures such as
    invalid arguments. `Nats.call()` raises it when the remote handler failed.
    """

    def __init__(self, message: str, code: int = 0) -> None:
        if code == RPC_OK:
            msg = f"RPC error code can't be {RPC_OK}, it means success"
            raise ValueError(msg)
        super().__init__(message)
        self.message: str = message
        self.code: int = code


def short_repr(value: Any) -> str:  # noqa: ANN401
    """Repr for logging, truncating large values"""
    return _repr.repr(value)


def log_call(method: str, args: Sequence[Any], kwargs: dict[str, Any]) -> str:
    """Format RPC call for logging, truncating large arguments"""
    formatted = [short_repr(arg) for arg in args]
    formatted.extend(f"{key}={short_repr(value)}" for key, value in kwargs.items())
    return f"{method}({', '.join(formatted)})"


def parse_request(payload: bytes) -> tuple[list[Any], dict[str, Any]]:
    """Decode `[args, kwargs]` RPC request payload"""
    data = decode_payload(payload)
    if data is INVALID:
        msg = "Invalid RPC request, payload is not JSON"
        raise RpcError(msg)
    if not isinstance(data, list) or len(data) != 2:  # noqa: PLR2004
        msg = "Invalid RPC request, payload must be [args, kwargs]"
        raise RpcError(msg)
    args, kwargs = data
    if not isinstance(args, list):
        msg = "Invalid RPC request, args must be a list"
        raise RpcError(msg)
    if not isinstance(kwargs, dict):
        msg = "Invalid RPC request, kwargs must be an object"
        raise RpcError(msg)
    return args, kwargs


def parse_reply(payload: bytes) -> Any:  # noqa: ANN401
    """Return result of `[1, result]` reply, raise RpcError for `[code, message]` or a malformed reply"""
    data = decode_payload(payload)
    if not isinstance(data, list) or len(data) != 2 or not isinstance(data[0], int):  # noqa: PLR2004
        msg = f"Invalid RPC reply: {short_repr(payload)}"
        raise RpcError(msg)
    status, result = data
    if status != RPC_OK:
        raise RpcError(str(result), code=status)
    return result


class RpcProxy:
    """Turn `nats.rpc.ami.originate(...)` into `nats.call("ami.originate", ...)`"""

    __slots__ = ("_nats", "_path")

    def __init__(self, nats: "Nats", path: tuple[str, ...]) -> None:
        self._nats = nats
        self._path = path

    def __getattr__(self, name: str) -> "RpcProxy":
        if name.startswith("__"):  # don't pretend to implement protocols like __await__
            raise AttributeError(name)
        return RpcProxy(self._nats, (*self._path, name))

    def __call__(self, *args: Any, **kwargs: Any) -> Awaitable[Any]:  # noqa: ANN401
        return self._nats.call(".".join(self._path), args, kwargs)
