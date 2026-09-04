"""Build an OpenAPI 3.0 spec from route registrations"""

import inspect
import re
from collections.abc import Callable
from typing import Annotated, Any, Optional, get_args, get_origin

import msgspec
from msgspec import Meta

from ._provider import Provider

# Mapping from Python built-in types to OpenAPI schema types.
_TYPE_MAP: dict[type, dict[str, str]] = {
    str: {"type": "string"},
    int: {"type": "integer"},
    float: {"type": "number"},
    bool: {"type": "boolean"},
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
        self.prefix: Optional[str] = ""  # where to mount /docs (don't automount them when None)
        self.title: str = "API"
        self.version: str = "0.0.0"
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
        """Build a JSON Schema for a return type, registering any nested structs as components."""
        if typ is bytes:
            return {"type": "string", "format": "binary"}
        (schema,), components = msgspec.json.schema_components(
            [typ], ref_template="#/components/schemas/{name}",
        )
        self._schemas.update(components)
        return schema

    def _example_for(self, schema: dict[str, Any]) -> Any:  # noqa: ANN401, PLR0911
        """Synthesize a representative example value from a JSON Schema.

        Swagger UI can't generate an example for JSON Schema tuple validation
        (``prefixItems``/``items: false``, which is how msgspec renders a Python
        ``tuple``) — it shows ``null`` for every slot instead. Build the example
        ourselves from field-level ``examples`` so tuple-shaped responses render.
        """
        if "$ref" in schema:
            name = schema["$ref"].rsplit("/", 1)[-1]
            return self._example_for(self._schemas.get(name, {}))
        if schema.get("examples"):
            return schema["examples"][0]
        if "example" in schema:
            return schema["example"]

        schema_type = schema.get("type")
        if schema_type == "object":
            return {name: self._example_for(prop) for name, prop in schema.get("properties", {}).items()}
        if schema_type == "array":
            if "prefixItems" in schema:
                return [self._example_for(item) for item in schema["prefixItems"]]
            items = schema.get("items")
            return [self._example_for(items)] if items else []

        return {"string": "", "integer": 0, "number": 0.0, "boolean": False}.get(schema_type)

    def add_route(  # noqa: PLR0913
        self,
        method: str,
        path: str,
        handler: Callable[..., Any],
        type_injectors: dict[type, Any],
        tag: Optional[str] = None,
        secure: Optional[bool] = None,
        content_type: Optional[str] = None,
    ) -> None:
        """Register a route in the spec.

        secure=True  — require auth on this endpoint (even if no global security).
        secure=False — mark this endpoint as public (overrides global security).
        secure=None  — inherit global security (default).
        """

        openapi_path = _PATH_PARAM_RE.sub(r"{\1}", path)
        path_param_names = _path_param_names(openapi_path)
        sig = inspect.signature(handler)

        parameters: list[dict[str, Any]] = []
        for param in sig.parameters.values():
            annotation: Any = param.annotation

            # Skip injected types — they are not user-facing parameters.
            if annotation in type_injectors:
                continue

            try:
                arg_type, optional, meta, _source = Provider.get_annotation(annotation)
            except TypeError:
                continue

            # Skip injected types wrapped in Annotated[type, Meta(...)].
            if arg_type in type_injectors:
                continue

            location = "path" if param.name in path_param_names else "query"
            default = None if param.default is inspect.Parameter.empty else param.default
            required = not optional and default is None

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

        return_type, return_description = _unwrap_return(sig.return_annotation)
        success: dict[str, Any] = {"description": return_description or "Success"}
        if sig.return_annotation is not inspect.Signature.empty:
            try:
                schema = self._response_schema(return_type)
            except TypeError:
                pass
            else:
                media_type: dict[str, Any] = {"schema": schema}
                if "prefixItems" in schema:
                    media_type["example"] = self._example_for(schema)
                success["content"] = {content_type or "application/json": media_type}

        summary, description = _split_docstring(handler.__doc__)
        operation: dict[str, Any] = {
            "summary": summary,
            "description": description,
            "operationId": handler.__qualname__,
            "parameters": parameters,
            "responses": {
                "200": success,
                "400": {"description": "Bad request — missing or invalid parameter"},
            },
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
        spec: dict[str, Any] = {
            "openapi": "3.2.0",
            "info": {"title": self.title, "version": self.version},
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
