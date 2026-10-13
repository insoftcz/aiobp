"""JSON codec and handler calling shared by events, RPC and JetStream consumers"""

import inspect
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

import msgspec
from nats.aio.msg import Msg

encoder = msgspec.json.Encoder()
decoder = msgspec.json.Decoder()
INVALID = object()  # decode_payload() result for a payload that isn't JSON


class _HeadersEventHandler(Protocol):
    def __call__(self, subject: str, data: Any, /, *, headers: dict[str, str]) -> Awaitable[None] | None: ...  # noqa: ANN401


# `headers` is passed only to handlers having a parameter of that name (or **kwargs)
EventHandler = Callable[[str, Any], Awaitable[None] | None] | _HeadersEventHandler


def decode_payload(payload: bytes) -> Any:  # noqa: ANN401
    """Decode JSON payload, return `INVALID` when it isn't JSON"""
    try:
        return decoder.decode(payload)
    except msgspec.DecodeError:
        return INVALID


async def invoke(handler: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
    """Call handler, awaiting it when it is a coroutine function"""
    result = handler(*args, **kwargs)
    if inspect.isawaitable(result):
        result = await result
    return result


def wants_headers(handler: EventHandler) -> bool:
    """Whether handler accepts `headers` keyword argument, detected once when subscribing"""
    try:
        parameters = inspect.signature(handler).parameters.values()
    except (TypeError, ValueError):  # no signature available, e.g. some builtins
        return False
    return any(
        parameter.kind == parameter.VAR_KEYWORD
        or (parameter.name == "headers" and parameter.kind in {parameter.POSITIONAL_OR_KEYWORD, parameter.KEYWORD_ONLY})
        for parameter in parameters
    )


async def invoke_event(handler: EventHandler, with_headers: bool, msg: Msg, data: Any) -> None:  # noqa: ANN401, FBT001
    """Call event handler, passing headers only when it wants them"""
    if with_headers:
        await invoke(handler, msg.subject, data, headers=msg.headers or {})
    else:
        await invoke(handler, msg.subject, data)
