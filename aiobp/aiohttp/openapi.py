"""Build an OpenAPI 3.0 spec from route registrations"""

import inspect
import re
from collections.abc import Callable
from typing import Any, Optional

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


class OpenAPIBuilder:
    """Accumulates route metadata and produces an OpenAPI 3.0 document."""

    def __init__(self, title: str = "API", version: str = "1.0.0") -> None:
        self._title: str = title
        self._version: str = version
        self._paths: dict[str, Any] = {}
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

    def add_route(  # noqa: PLR0913
        self,
        method: str,
        path: str,
        handler: Callable[..., Any],
        type_injectors: dict[type, Any],
        tag: Optional[str] = None,
        secure: Optional[bool] = None,
    ) -> None:
        """Register a route in the spec.

        secure=True  — require auth on this endpoint (even if no global security).
        secure=False — mark this endpoint as public (overrides global security).
        secure=None  — inherit global security (default).
        """
        from .provider import Provider

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
                arg_type, optional, meta = Provider.get_annotation(annotation)
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
            if meta.description:
                entry["description"] = meta.description
            if default is not None:
                entry["schema"] = {**entry["schema"], "default": default}

            parameters.append(entry)

        operation: dict[str, Any] = {
            "summary": (handler.__doc__ or "").strip().splitlines()[0] if handler.__doc__ else "",
            "description": handler.__doc__ or "",
            "operationId": handler.__qualname__,
            "parameters": parameters,
            "responses": {
                "200": {"description": "Success"},
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
        spec: dict[str, Any] = {
            "openapi": "3.0.0",
            "info": {"title": self._title, "version": self._version},
            "paths": self._paths,
        }
        if self._security_schemes:
            spec["components"] = {"securitySchemes": self._security_schemes}
        if self._global_security:
            spec["security"] = self._global_security
        return spec

    @property
    def swagger_ui_html(self) -> str:
        return """<!DOCTYPE html>
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
    SwaggerUIBundle({ url: "/openapi.json", dom_id: "#swagger-ui" });
  </script>
</body>
</html>"""
