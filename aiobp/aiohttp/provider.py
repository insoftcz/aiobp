"""Provide arguments from request based on method annotations"""

import inspect
from collections.abc import Callable
from functools import partial
from types import NoneType
from typing import Annotated, Any, Optional, TypeVar, Union, get_args, get_origin

from aiohttp import web
from msgspec import Meta

T = TypeVar("T", int, str)
D = TypeVar("D")


class Provider:
    """Provide arguments from request based on method annotations"""

    def __init__(self, handler: Callable, type_injectors: dict[type[D], Callable[[web.Request], D]]):
        self.__type_injectors = type_injectors
        self.__args = {}
        self.__inspect(handler)

    def __inspect(self, handler: Callable) -> None:
        sig = inspect.signature(handler)
        errors = []
        for param in sig.parameters.values():
            try:
                arg_type, optional, meta = self.get_annotatation(param.annotation)
                default = None if param.default is inspect.Signature.empty else param.default
                sources = [self.__get_from_path, self.__get_from_query]

                # dependency injection factory
                factory = self.__type_injectors.get(arg_type)
                if factory:
                    self.__args[param.name] = factory
                    continue

                self.__args[param.name] = partial(
                    self.__getter,
                    param.name,
                    arg_type,
                    optional=optional,
                    default=default,
                    sources=sources,
                )
            except TypeError:  # noqa: PERF203 - need to collect all validation errors
                msg = f'Argument "{param.name}" of handler "{handler.__qualname__}" at {param.name} '
                "is not Annotated[type, msgspec.Meta]!"
                errors.append(msg)

        if errors:
            raise TypeError(errors)

    def __getter(
        self,
        name: str,
        typ: type[T],
        *,
        optional: bool,
        default: Optional[T],
        sources: list[Callable[[str, web.Request], Optional[str]]],
        request: web.Request,
    ) -> Optional[T]:
        """Take value for one argument from source and validate it"""
        for source in sources:
            value = source(name, request)
            if value:
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
    def __get_from_path(key: str, request: web.Request) -> Optional[str]:
        return request.match_info.get(key)

    @staticmethod
    def __get_from_query(key: str, request: web.Request) -> Optional[str]:
        return request.query.get(key)

    @staticmethod
    def get_annotatation(hint: type) -> tuple[type, bool, Meta]:
        hint_origin = get_origin(hint)
        optional = False
        if hint_origin is Union:
            # Optional[Annotated[str, "something"]] -> Union[Annotated[str], None]
            hint, none = get_args(hint)
            if none is not NoneType:
                raise TypeError

            hint_origin = get_origin(hint)
            optional = True

        if hint_origin is not Annotated:
            raise TypeError

        hint_type, meta = get_args(hint)
        if not isinstance(meta, Meta):
            raise TypeError

        return hint_type, optional, meta

    async def gather_args(self, request: web.Request) -> dict[str, Any]:
        args = []
        errors = []
        for key, getter in self.__args.items():
            try:
                args.append((key, getter(request=request)))
            except ValueError as error:  # noqa: PERF203 - need to collect all validation errors
                errors.append((key, str(error)))

        if errors:
            raise TypeError(errors)

        return dict(args)

    def encode_response(self, result: Any) -> web.StreamResponse:
        return web.Response(body=result)
