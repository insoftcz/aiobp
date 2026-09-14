"""Build an OpenAPI 3.0 spec from route registrations"""

import inspect
import re
from collections.abc import Callable
from typing import Annotated, Any, Optional, get_args, get_origin

import msgspec
from msgspec import Meta

from aiobp.aiohttp._provider import ApiError, Provider, RequestValidationError, ServerError, SourceKind

# Mapping from Python built-in types to OpenAPI schema types.
_TYPE_MAP: dict[type, dict[str, str]] = {
    str: {"type": "string"},
    int: {"type": "integer"},
    float: {"type": "number"},
    bool: {"type": "boolean"},
}

# SourceKind -> OpenAPI parameter "in" location, for the single-value sources.
# BODY/BODY_KEY become requestBody instead; PATH_ITEMS/QUERY_ITEMS expand into
# one parameter per struct/TypedDict field (see add_route).
_PARAM_LOCATION_BY_KIND: dict[SourceKind, str] = {
    SourceKind.PATH: "path",
    SourceKind.QUERY: "query",
    SourceKind.HEADER: "header",
    SourceKind.COOKIE: "cookie",
}

_PATH_PARAM_RE = re.compile(r"\{(\w+)\}")


def _schema_for(typ: type) -> dict[str, str]:
    return _TYPE_MAP.get(typ, {"type": "string"})


def _path_param_names(path: str) -> set[str]:
    return set(_PATH_PARAM_RE.findall(path))


def _split_docstring(doc: Optional[str]) -> tuple[str, str]:
    """Split a handler docstring into (summary, description).

    Dedents using the common indentation of the description lines (like
    ``inspect.cleandoc``), rather than stripping all leading whitespace,
    so an indented code example in the docstring doesn't get flattened.
    """
    if not doc:
        return "", ""
    summary, _, rest = inspect.cleandoc(doc).partition("\n")
    return summary, rest.strip("\n")


def _clean_description(doc: Optional[str]) -> str:
    """Dedent a docstring-derived description (e.g. from a msgspec Struct) into one string.

    JSON Schema's ``description`` has no separate summary/description split like an
    OpenAPI operation does, so this reuses ``_split_docstring`` and joins the parts
    back together — the point is the dedenting, not the split.
    """
    summary, description = _split_docstring(doc)
    if not description:
        return summary
    return f"**{summary}**\n___\n{description}"


def _unwrap_return(annotation: Any) -> tuple[Any, Optional[str]]:
    """Split a return annotation into (type, description), unwrapping Annotated[type, Meta(...)]."""
    if get_origin(annotation) is not Annotated:
        return annotation, None
    typ, *rest = get_args(annotation)
    description = next((arg.description for arg in rest if isinstance(arg, Meta) and arg.description), None)
    return typ, description


class OpenAPIBuilder:
    """Accumulates route metadata and produces an OpenAPI 3.0 document."""

    def __init__(self) -> None:
        self.title: str = "API"
        self.version: str = "0.0.0"
        self.description: Optional[str] = None
        self._paths: dict[str, Any] = {}
        self._schemas: dict[str, Any] = {}
        self._security_schemes: dict[str, Any] = {}
        self._global_security: list[dict[str, list[str]]] = []

    def add_bearer_auth(self, *, global_security: bool = True) -> None:
        """Add HTTP Bearer token authentication (Authorization: Bearer <token>)."""
        self._security_schemes["BearerAuth"] = {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "JWT",
        }
        if global_security:
            self._global_security.append({"BearerAuth": []})

    def add_oauth2(
        self,
        token_url: str,
        scopes: Optional[dict[str, str]] = None,
        *,
        global_security: bool = True,
    ) -> None:
        """Add OAuth2 password/client-credentials flow with a token endpoint."""
        self._security_schemes["OAuth2"] = {
            "type": "oauth2",
            "flows": {
                "clientCredentials": {
                    "tokenUrl": token_url,
                    "scopes": scopes or {},
                },
            },
        }
        if global_security:
            self._global_security.append({"OAuth2": list((scopes or {}).keys())})

    def _response_schema(self, typ: Any) -> dict[str, Any]:
        """Build a JSON Schema for a type, registering any nested structs as components."""
        if typ is bytes:
            return {"type": "string", "format": "binary"}
        (schema,), components = msgspec.json.schema_components(
            [typ], ref_template="#/components/schemas/{name}",
        )
        # msgspec pulls Struct/TypedDict docstrings straight into "description" with
        # their original class-body indentation intact — dedent it, or Swagger UI's
        # Markdown renderer treats the indentation as a code block.
        for component in components.values():
            if "description" in component:
                component["description"] = _clean_description(component["description"])
        if "description" in schema:
            schema["description"] = _clean_description(schema["description"])

        for name, component in components.items():
            existing = self._schemas.get(name)
            if existing is not None and existing != component:
                msg = (
                    f"OpenAPI schema name collision: two different types are both named {name!r}. "
                    f"Rename one of them so their generated JSON Schemas don't clash."
                )
                raise ValueError(msg)

        self._schemas.update(components)
        return schema

    def _response_entry(self, typ: Any) -> dict[str, Any]:  # noqa: ANN401
        """Build a full OpenAPI response object (description + schema + example) for a type.

        ``typ`` may be an ``ApiError`` subclass — the response-level
        description comes from *its own* docstring (what the error means),
        while its ``response_type`` (the msgspec.Struct describing its body)
        supplies the schema, so callers can pass the exception class itself
        wherever a response type is expected.
        """
        summary, _ = _split_docstring(typ.__doc__)
        if isinstance(typ, type) and issubclass(typ, ApiError):
            typ = typ.response_type
        schema = self._response_schema(typ)
        media_type: dict[str, Any] = {"schema": schema, "example": self._example_for(schema)}
        return {"description": summary or "Error", "content": {"application/json": media_type}}

    def _resolve_schema(self, schema: dict[str, Any]) -> dict[str, Any]:
        """Follow a single ``$ref`` into the registered component schemas."""
        if "$ref" in schema:
            name = schema["$ref"].rsplit("/", 1)[-1]
            return self._schemas.get(name, {})
        return schema

    def _example_for(self, schema: dict[str, Any]) -> Any:  # noqa: ANN401, PLR0911
        """Synthesize a representative example value from a JSON Schema.

        Swagger UI's own example generation has two known blind spots this works
        around: it can't handle JSON Schema tuple validation (``prefixItems``,
        how msgspec renders a Python ``tuple``) — showing ``null`` for every
        slot — and for an ``Optional[T]`` field (rendered as ``anyOf: [T, {type:
        null}]``) it uses the field's top-level ``default`` (``null``) instead of
        digging into the non-null branch for its ``description``/``examples``.
        """
        schema = self._resolve_schema(schema)
        if schema.get("examples"):
            return schema["examples"][0]
        if "example" in schema:
            return schema["example"]

        if "anyOf" in schema:
            for branch in schema["anyOf"]:
                if branch.get("type") != "null":
                    return self._example_for(branch)
            return None

        if "enum" in schema:
            return schema["enum"][0]

        schema_type = schema.get("type")
        if schema_type == "object":
            return {name: self._example_for(prop) for name, prop in schema.get("properties", {}).items()}
        if schema_type == "array":
            if "prefixItems" in schema:
                return [self._example_for(item) for item in schema["prefixItems"]]
            items = schema.get("items")
            return [self._example_for(items)] if items else []

        return {"string": "", "integer": 0, "number": 0.0, "boolean": False}.get(schema_type)

    def add_route(  # noqa: C901, PLR0912, PLR0913, PLR0915
        self,
        method: str,
        path: str,
        handler: Callable[..., Any],
        type_injectors: dict[type, Any],
        tag: Optional[str] = None,
        secure: Optional[bool] = None,
        content_type: Optional[str] = None,
        responses: Optional[dict[int, type]] = None,
    ) -> None:
        """Register a route in the spec.

        secure=True  — require auth on this endpoint (even if no global security).
        secure=False — mark this endpoint as public (overrides global security).
        secure=None  — inherit global security (default).

        Every route documents built-in 400 (argument validation failed) and 500
        (unhandled exception) responses automatically, matching the router's
        actual runtime behaviour. ``responses`` adds/overrides entries for other
        status codes a handler can return, e.g. ``responses={404: NotFoundError}``.
        """
        openapi_path = _PATH_PARAM_RE.sub(r"{\1}", path)
        path_param_names = _path_param_names(openapi_path)
        sig = inspect.signature(handler)

        parameters: list[dict[str, Any]] = []
        request_body_schema: Optional[dict[str, Any]] = None
        body_key_properties: dict[str, Any] = {}
        body_key_required: list[str] = []

        for param in sig.parameters.values():
            annotation: Any = param.annotation

            # Skip injected types — they are not user-facing parameters.
            if annotation in type_injectors:
                continue

            try:
                arg_type, optional, meta, source = Provider.get_annotation(annotation)
            except TypeError:
                continue

            # Skip injected types wrapped in Annotated[type, Meta(...)].
            if arg_type in type_injectors:
                continue

            default = None if param.default is inspect.Parameter.empty else param.default
            required = not optional and default is None
            kind = source.kind if source is not None else None

            if kind == SourceKind.BODY:
                # Whole request body — takes precedence over any BodyKey fields below.
                request_body_schema = self._response_schema(arg_type)
                continue

            if kind == SourceKind.BODY_KEY:
                # One named field of the body; merged into a synthesized object schema.
                field_schema = dict(_schema_for(arg_type))
                if meta is not None and meta.description:
                    field_schema["description"] = meta.description
                if meta is not None and meta.examples:
                    field_schema["example"] = meta.examples[0]
                if default is not None:
                    field_schema["default"] = default
                body_key_properties[param.name] = field_schema
                if required:
                    body_key_required.append(param.name)
                continue

            if kind in (SourceKind.PATH_ITEMS, SourceKind.QUERY_ITEMS):
                # Whole path/query mapping — expand the struct/TypedDict into one
                # parameter per field, since OpenAPI has no "bulk" parameter concept.
                location = "path" if kind == SourceKind.PATH_ITEMS else "query"
                obj_schema = self._resolve_schema(self._response_schema(arg_type))
                required_fields = set(obj_schema.get("required", []))
                for field_name, field_schema in obj_schema.get("properties", {}).items():
                    parameters.append({
                        "name": field_name,
                        "in": location,
                        "required": location == "path" or field_name in required_fields,
                        "schema": field_schema,
                    })
                continue

            if kind is None:
                location = "path" if param.name in path_param_names else "query"
            else:
                location = _PARAM_LOCATION_BY_KIND.get(kind, "query")

            entry: dict[str, Any] = {
                "name": param.name,
                "in": location,
                "required": required,
                "schema": _schema_for(arg_type),
            }
            if meta is not None and meta.description:
                entry["description"] = meta.description
            if default is not None:
                entry["schema"] = {**entry["schema"], "default": default}
            if meta is not None and meta.examples:
                entry["schema"] = {**entry["schema"], "example": meta.examples[0]}

            parameters.append(entry)

        if request_body_schema is None and body_key_properties:
            request_body_schema = {"type": "object", "properties": body_key_properties}
            if body_key_required:
                request_body_schema["required"] = body_key_required

        return_type, return_description = _unwrap_return(sig.return_annotation)
        success: dict[str, Any] = {"description": return_description or "Success"}
        if sig.return_annotation is not inspect.Signature.empty:
            try:
                schema = self._response_schema(return_type)
            except TypeError:
                pass
            else:
                media_type: dict[str, Any] = {"schema": schema, "example": self._example_for(schema)}
                success["content"] = {content_type or "application/json": media_type}

        summary, description = _split_docstring(handler.__doc__)
        operation: dict[str, Any] = {
            "summary": summary,
            "description": description,
            "operationId": handler.__qualname__,
            "parameters": parameters,
            "responses": {
                "200": success,
                "400": self._response_entry(RequestValidationError),
                "500": self._response_entry(ServerError),
            },
        }
        for status, error_type in (responses or {}).items():
            operation["responses"][str(status)] = self._response_entry(error_type)
        if request_body_schema is not None:
            operation["requestBody"] = {
                "required": True,
                "content": {"application/json": {
                    "schema": request_body_schema,
                    "example": self._example_for(request_body_schema),
                }},
            }
        if tag:
            operation["tags"] = [tag]
        if secure is True:
            operation["security"] = self._global_security or [{next(iter(self._security_schemes)): []}]
        elif secure is False:
            operation["security"] = []  # explicitly public — no auth required

        self._paths.setdefault(openapi_path, {})[method.lower()] = operation

    def build(self) -> dict[str, Any]:
        # 3.2 (not 3.0) because schemas come straight from msgspec.json.schema, which speaks
        # JSON Schema 2020-12 — e.g. prefixItems for tuples, plural "examples" — and OpenAPI
        # 3.0's Schema Object (JSON-Schema-draft-4-ish) can't represent either.
        info: dict[str, Any] = {"title": self.title, "version": self.version}
        if self.description:
            info["description"] = _clean_description(self.description)
        spec: dict[str, Any] = {
            "openapi": "3.2.0",
            "info": info,
            "paths": self._paths,
        }
        components: dict[str, Any] = {}
        if self._security_schemes:
            components["securitySchemes"] = self._security_schemes
        if self._schemas:
            components["schemas"] = self._schemas
        if components:
            spec["components"] = components
        if self._global_security:
            spec["security"] = self._global_security
        return spec

    def swagger_ui_html(self, url: str) -> str:
        return f"""<!DOCTYPE html>
<html>
<head>
  <title>Swagger UI</title>
  <meta charset="utf-8"/>
  <link rel="stylesheet" href="https://unpkg.com/swagger-ui-dist/swagger-ui.css">
</head>
<body>
  <div id="swagger-ui"></div>
  <script src="https://unpkg.com/swagger-ui-dist/swagger-ui-bundle.js"></script>
  <script>
    SwaggerUIBundle({{
      url: "{url}",
      dom_id: "#swagger-ui",
      persistAuthorization: true
    }});
  </script>
</body>
</html>"""
