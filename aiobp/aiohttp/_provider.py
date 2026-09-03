"""Provide arguments from request based on method annotations"""

import inspect
from collections.abc import Callable
from functools import partial
from typing import TYPE_CHECKING, Annotated, Any, Optional, Union, final, get_args, get_origin, get_type_hints

import msgspec
from aiohttp import web
from msgspec import Meta
from typing_extensions import is_typeddict, override

# Factory that resolves an injected dependency from the current request.
InjectorFactory = Callable[[web.Request], Any]
# Getter stored per argument: either an InjectorFactory or a partial __getter call.
ArgGetter = Callable[..., Any]


@final
class Source:
    """Annotation marker that controls where a handler argument is resolved from.

    Use the subscript form in handler signatures::

        who: PathKey[str, "someone"]      # type str, description "someone"
        page: QueryKey[int, "page size"]
        item: FromBody[Item, "payload"]
        grant_type: BodyKey[str, "OAuth grant type"]
        token: HeaderKey[str, "auth token"]
        session: CookieKey[str, "session id"]

    ``PathKey``/``QueryKey``/``HeaderKey``/``CookieKey``/``BodyKey`` resolve a
    single named value. ``FromBody`` always decodes the entire request body
    into ``Item``. ``FromPath``/``FromQuery`` likewise always consume the
    *entire* path/query mapping into a ``msgspec.Struct`` or ``TypedDict``::

        class Paging(TypedDict):
            limit: int
            offset: int

        async def list_items(paging: FromQuery[Paging, "pagination"]) -> ...: ...

    At runtime the subscript expands to ``Annotated[type, Meta(description), Source]``.
    For static type checkers ``PathKey`` aliases ``Annotated``, so the declared
    parameter type is preserved.
    """

    __slots__ = ("kind",)

    def __init__(self, kind: str) -> None:
        self.kind = kind

    @override
    def __repr__(self) -> str:
        return self.kind.capitalize()

    def __getitem__(self, params: Any) -> Any:
        """Expand ``Source[type, description]`` into an ``Annotated`` hint.

        The second element may be a plain string, a ``typing_extensions.Doc``,
        or a ``msgspec.Meta`` / ``Param(...)`` carrying validation constraints.
        """
        if isinstance(params, tuple):
            typ, doc = params
            if isinstance(doc, Meta):
                return Annotated[typ, doc, self]
            description = getattr(doc, "documentation", doc)
            return Annotated[typ, Meta(description=description), self]
        return Annotated[params, self]


if TYPE_CHECKING:
    # Static type checkers resolve PathKey[str, "desc"] as Annotated[str, "desc"] → str.
    from typing import Annotated as BodyKey  # pyright: ignore[reportUnusedImport]
    from typing import Annotated as CookieKey  # pyright: ignore[reportUnusedImport]
    from typing import Annotated as FromBody  # pyright: ignore[reportUnusedImport]
    from typing import Annotated as FromPath  # pyright: ignore[reportUnusedImport]
    from typing import Annotated as FromQuery  # pyright: ignore[reportUnusedImport]
    from typing import Annotated as HeaderKey  # pyright: ignore[reportUnusedImport]
    from typing import Annotated as PathKey  # pyright: ignore[reportUnusedImport]
    from typing import Annotated as QueryKey  # pyright: ignore[reportUnusedImport]
else:
    PathKey = Source("path")
    QueryKey = Source("query")
    HeaderKey = Source("header")
    CookieKey = Source("cookie")
    BodyKey = Source("body_key")
    FromBody = Source("body")
    FromPath = Source("path_items")
    FromQuery = Source("query_items")


def Param(description: str, *, source: Optional[str] = None, **kwargs: Any) -> Meta:  # noqa: N802
    """Build ``msgspec.Meta`` with the description as first positional argument.

    Usage::

        who: PathKey[str, Param("Name to greet", min_length=5)]

    Pass ``source`` to look the value up under a different name than the
    argument itself, e.g. a header that isn't a valid Python identifier::

        content_type: HeaderKey[str, Param("Call unique ID", source="Content-Type")]
    """
    extra = kwargs.pop("extra", None)
    if source is not None:
        extra = {**(extra or {}), "source": source}
    return Meta(description=description, extra=extra, **kwargs)


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
                arg_type, optional, meta, source = self.get_annotation(annotation)
                default: Any = None if param.default is inspect.Signature.empty else param.default
                # Also allow injected types wrapped in Annotated[type, Meta(...)].
                factory = self._type_injectors.get(arg_type)
                if factory:
                    self._args[param.name] = factory
                    continue

                if source is not None and source.kind == "body":
                    self._args[param.name] = partial(
                        self._get_body, param.name, arg_type, optional=optional, default=default,
                    )
                elif source is not None and source.kind == "body_key":
                    self._args[param.name] = partial(
                        self._get_body_key,
                        param.name,
                        arg_type,
                        optional=optional,
                        default=default,
                        meta=meta,
                        source_name=self._source_name(param.name, meta),
                    )
                elif source is not None and source.kind in ("path_items", "query_items"):
                    get_mapping = self._get_path_mapping if source.kind == "path_items" else self._get_query_mapping
                    self._args[param.name] = partial(
                        self._get_mapping, param.name, get_mapping, arg_type, optional=optional, default=default,
                    )
                else:
                    self._args[param.name] = partial(
                        self._getter,
                        param.name,
                        arg_type,
                        optional=optional,
                        default=default,
                        sources=self._sources_for(source),
                        meta=meta,
                        source_name=self._source_name(param.name, meta),
                    )
            except TypeError:
                msg = (
                    f'Argument "{param.name}" of handler "{handler.__qualname__}"'
                    f' at {param.name} is not Annotated[type, msgspec.Meta]!'
                )
                errors.append(msg)

        if errors:
            raise TypeError(errors)

    def _sources_for(self, source: Optional[Source]) -> list[Callable[[str, web.Request], Optional[str]]]:
        """Pick the getter(s) to try for a non-body argument, in order."""
        if source is None:
            return [self._get_from_path, self._get_from_query]
        by_kind: dict[str, Callable[[str, web.Request], Optional[str]]] = {
            "path": self._get_from_path,
            "query": self._get_from_query,
            "header": self._get_from_header,
            "cookie": self._get_from_cookie,
        }
        return [by_kind[source.kind]]

    @staticmethod
    def _source_name(name: str, meta: Optional[Meta]) -> str:
        """Resolve the key used to look the value up, honouring ``Param(source=...)``."""
        if meta is not None and meta.extra:
            return meta.extra.get("source", name)
        return name

    def _getter(  # noqa: PLR0913
        self,
        name: str,
        typ: type,
        *,
        optional: bool,
        default: Any,
        sources: list[Callable[[str, web.Request], Optional[str]]],
        request: web.Request,
        meta: Optional[Meta] = None,
        source_name: Optional[str] = None,
    ) -> Any:
        """Take value for one argument from source and validate it"""
        key = source_name or name
        value: Any = None
        for source in sources:
            value = source(key, request)
            if value is not None:
                break
        return self._format_value(name, typ, value, optional=optional, default=default, meta=meta)

    @staticmethod
    def _format_value(  # noqa: PLR0913
        name: str, typ: type, value: Any, *, optional: bool, default: Any, meta: Optional[Meta],
    ) -> Any:
        """Apply the default fallback, then validate/coerce a single raw value.

        A present-but-empty value (e.g. a bare ``?attachments`` query flag with no
        ``=value``) can't be meaningfully coerced into anything but ``str``, so it's
        treated the same as a missing value for every other type.
        """
        if value is None or (value == "" and typ is not str):
            value = default
        if value is None:
            if optional:
                return None
            msg = f"Missing required value {name}"
            raise TypeError(msg)

        if meta is not None:
            try:
                return msgspec.convert(value, type=Annotated[typ, meta], strict=False)
            except msgspec.ValidationError as error:
                msg = f"Invalid value for {name}: {error}"
                raise TypeError(msg) from error

        return typ(value)

    async def _get_body_key(  # noqa: PLR0913
        self,
        name: str,
        typ: type,
        *,
        optional: bool,
        default: Any,
        meta: Optional[Meta],
        source_name: Optional[str],
        request: web.Request,
    ) -> Any:
        """Resolve a handler argument from a single named field in the request body."""
        key = source_name or name
        content_type = request.content_type or "application/json"
        try:
            if "json" in content_type:
                raw = await request.read()
                body = msgspec.json.decode(raw) if raw else None
                value = body.get(key) if isinstance(body, dict) else None
            else:
                form = await request.post()
                value = form.get(key)
        except msgspec.DecodeError as exc:
            raise TypeError(str(exc)) from exc

        return self._format_value(name, typ, value, optional=optional, default=default, meta=meta)

    async def _get_body(
        self, name: str, typ: type, *, optional: bool, default: Any, request: web.Request,
    ) -> Any:
        """Resolve a handler argument by decoding the entire request body."""
        if typ is bytes:
            return await request.read()

        content_type = request.content_type or "application/json"
        try:
            if "json" in content_type:
                raw = await request.read()
                if not raw:
                    if optional:
                        return default
                    msg = f"Missing required body for {name}"
                    raise TypeError(msg)
                return msgspec.json.decode(raw, type=typ)

            # form-urlencoded or multipart → convert the whole form into typ,
            # keeping uploaded files as FileField and repeated fields as lists
            form = await request.post()
            data = self._mapping_to_dict(form, typ)
            if not data:
                if optional:
                    return default
                msg = f"Missing required body for {name}"
                raise TypeError(msg)
            return msgspec.convert(data, type=typ, strict=False)

        except (msgspec.DecodeError, msgspec.ValidationError) as exc:
            raise TypeError(str(exc)) from exc

    def _get_mapping(  # noqa: PLR0913
        self,
        name: str,
        get_mapping: Callable[[web.Request], Any],
        typ: type,
        *,
        optional: bool,
        default: Any,
        request: web.Request,
    ) -> Any:
        """Resolve a handler argument by converting an entire request mapping (path or query)."""
        data = self._mapping_to_dict(get_mapping(request), typ)
        if not data:
            if optional:
                return default
            msg = f"Missing required value {name}"
            raise TypeError(msg)

        try:
            return msgspec.convert(data, type=typ, strict=False)
        except msgspec.ValidationError as exc:
            raise TypeError(str(exc)) from exc

    @staticmethod
    def _mapping_to_dict(mapping: Any, typ: type) -> dict[str, Any]:
        """Build a dict from a request mapping, gathering list-typed fields via getall() where supported."""
        if isinstance(typ, type) and issubclass(typ, msgspec.Struct):
            fields = {field.name: field.type for field in msgspec.structs.fields(typ)}
        elif is_typeddict(typ):
            fields = get_type_hints(typ)
        else:
            return dict(mapping)

        getall = getattr(mapping, "getall", None)
        data: dict[str, Any] = {}
        for name, field_type in fields.items():
            if name not in mapping:
                continue
            data[name] = getall(name) if getall is not None and get_origin(field_type) is list else mapping.get(name)
        return data

    @staticmethod
    def _get_path_mapping(request: web.Request) -> Any:
        return request.match_info

    @staticmethod
    def _get_query_mapping(request: web.Request) -> Any:
        return request.query

    @staticmethod
    def _get_from_path(key: str, request: web.Request) -> Optional[str]:
        return request.match_info.get(key)

    @staticmethod
    def _get_from_query(key: str, request: web.Request) -> Optional[str]:
        return request.query.get(key)

    @staticmethod
    def _get_from_header(key: str, request: web.Request) -> Optional[str]:
        return request.headers.get(key)

    @staticmethod
    def _get_from_cookie(key: str, request: web.Request) -> Optional[str]:
        return request.cookies.get(key)

    @staticmethod
    def get_annotation(hint: Any) -> tuple[type, bool, Optional[Meta], Optional[Source]]:
        """Parse Annotated[type, Meta?, Source?] with optional Optional wrapper.

        Returns (type, is_optional, meta_or_none, source_or_none).
        Raises TypeError if the hint is not an ``Annotated`` form.
        """
        # get_origin/get_args return Any — that is a stdlib typing limitation.
        hint_origin: Any = get_origin(hint)
        optional = False
        if hint_origin is Union:
            # Optional[Annotated[str, Meta(...)]] -> Union[Annotated[str, Meta(...)], None]
            args: Any = get_args(hint)
            hint, none = args[0], args[1]
            if none is not type(None):
                raise TypeError

            hint_origin = get_origin(hint)
            optional = True

        if hint_origin is not Annotated:
            raise TypeError

        annotated_args: Any = get_args(hint)
        if len(annotated_args) < 2:  # noqa: PLR2004
            raise TypeError

        hint_type: Any = annotated_args[0]
        meta: Any = None
        source: Optional[Source] = None

        for arg in annotated_args[1:]:
            if isinstance(arg, Meta):
                meta = arg
            elif isinstance(arg, Source):
                source = arg

        if meta is None and source is None:
            raise TypeError

        return hint_type, optional, meta, source

    async def gather_args(self, request: web.Request) -> dict[str, Any]:
        args: list[tuple[str, Any]] = []
        errors: list[tuple[str, str]] = []
        for key, getter in self._args.items():
            try:
                result = getter(request=request)
                if inspect.isawaitable(result):
                    result = await result
                args.append((key, result))
            except ValueError as error:
                errors.append((key, str(error)))

        if errors:
            raise TypeError(errors)

        return dict(args)

    def encode_response(
        self, result: Any, content_type: Optional[str] = None, charset: str = "utf-8", status: int = 200,
    ) -> web.StreamResponse:
        if isinstance(result, web.StreamResponse):
            return result
        if content_type is not None:
            if isinstance(result, bytes):
                return web.Response(body=result, content_type=content_type, status=status)
            if "json" in content_type:
                return web.Response(
                    body=msgspec.json.encode(result), content_type=content_type, charset=charset, status=status,
                )
            return web.Response(text=str(result), content_type=content_type, charset=charset, status=status)
        if isinstance(result, str):
            return web.Response(text=result, content_type="text/html", charset=charset, status=status)
        return web.Response(body=result, status=status)
