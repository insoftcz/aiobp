"""Provide arguments from request based on method annotations"""

import inspect
from collections.abc import Callable
from functools import partial
from types import NoneType
from typing import Annotated, Any, Optional, Union, get_args, get_origin

from aiohttp import web
from msgspec import Meta

# Factory that resolves an injected dependency from the current request.
InjectorFactory = Callable[[web.Request], Any]
# Getter stored per argument: either an InjectorFactory or a partial __getter call.
ArgGetter = Callable[..., Any]


class Provider:
    """Provide arguments from request based on method annotations"""

    def __init__(
        self,
        handler: Callable[..., Any],
        type_injectors: dict[type, InjectorFactory],
    ) -> None:
        self._type_injectors: dict[type, InjectorFactory] = type_injectors
        self._args: dict[str, ArgGetter] = {}
        self._inspect(handler)

    def _inspect(self, handler: Callable[..., Any]) -> None:
        sig = inspect.signature(handler)
        errors: list[str] = []
        for param in sig.parameters.values():
            # annotation is Any by nature of inspect — unavoidable.
            annotation: Any = param.annotation
            # Check injector registry first using the raw annotation (plain type, no Annotated required).
            factory = self._type_injectors.get(annotation)
            if factory:
                self._args[param.name] = factory
                continue

            try:
                arg_type, optional, _meta = self.get_annotation(annotation)
                default: Any = None if param.default is inspect.Signature.empty else param.default
                sources: list[Callable[[str, web.Request], Optional[str]]] = [
                    self._get_from_path,
                    self._get_from_query,
                ]

                # Also allow injected types wrapped in Annotated[type, Meta(...)].
                factory = self._type_injectors.get(arg_type)
                if factory:
                    self._args[param.name] = factory
                    continue

                self._args[param.name] = partial(
                    self._getter,
                    param.name,
                    arg_type,
                    optional=optional,
                    default=default,
                    sources=sources,
                )
            except TypeError:
                msg = (
                    f'Argument "{param.name}" of handler "{handler.__qualname__}"'
                    f' at {param.name} is not Annotated[type, msgspec.Meta]!'
                )
                errors.append(msg)

        if errors:
            raise TypeError(errors)

    def _getter(  # noqa: PLR0913
        self,
        name: str,
        typ: type,
        *,
        optional: bool,
        default: Any,
        sources: list[Callable[[str, web.Request], Optional[str]]],
        request: web.Request,
    ) -> Any:
        """Take value for one argument from source and validate it"""
        value: Any = None
        for source in sources:
            value = source(name, request)
            if value is not None:
                break
        else:
            value = default

        if value is None:
            if optional:
                return None

            msg = f"Missing required value {name}"
            raise TypeError(msg)

        return typ(value)

    @staticmethod
    def _get_from_path(key: str, request: web.Request) -> Optional[str]:
        return request.match_info.get(key)

    @staticmethod
    def _get_from_query(key: str, request: web.Request) -> Optional[str]:
        return request.query.get(key)

    @staticmethod
    def get_annotation(hint: Any) -> tuple[type, bool, Meta]:
        """Parse Annotated[type, Meta(...)] with optional Optional wrapper.

        Returns (type, is_optional, meta). Raises TypeError if the hint does
        not conform to the expected structure.
        """
        # get_origin/get_args return Any — that is a stdlib typing limitation.
        hint_origin: Any = get_origin(hint)
        optional = False
        if hint_origin is Union:
            # Optional[Annotated[str, Meta(...)]] -> Union[Annotated[str, Meta(...)], None]
            args: Any = get_args(hint)
            hint, none = args[0], args[1]
            if none is not NoneType:
                raise TypeError

            hint_origin = get_origin(hint)
            optional = True

        if hint_origin is not Annotated:
            raise TypeError

        hint_type: Any
        meta: Any
        hint_type, meta = get_args(hint)
        if not isinstance(meta, Meta):
            raise TypeError

        return hint_type, optional, meta

    async def gather_args(self, request: web.Request) -> dict[str, Any]:
        args: list[tuple[str, Any]] = []
        errors: list[tuple[str, str]] = []
        for key, getter in self._args.items():
            try:
                args.append((key, getter(request=request)))
            except ValueError as error:
                errors.append((key, str(error)))

        if errors:
            raise TypeError(errors)

        return dict(args)

    def encode_response(self, result: Any, content_type: Optional[str] = None) -> web.StreamResponse:
        if isinstance(result, web.StreamResponse):
            return result
        if content_type is not None:
            if isinstance(result, bytes):
                return web.Response(body=result, content_type=content_type)
            return web.Response(text=str(result), content_type=content_type)
        if isinstance(result, str):
            return web.Response(text=result, content_type="text/html")
        return web.Response(body=result)
