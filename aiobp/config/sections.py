"""Nested configuration sections"""

import dataclasses
from typing import Any, TypeGuard, get_type_hints

import msgspec


def is_section(hint: object) -> TypeGuard[type]:
    """Dataclass or msgspec Struct, i.e. nested configuration section"""
    return isinstance(hint, type) and (dataclasses.is_dataclass(hint) or issubclass(hint, msgspec.Struct))


def _fields(config_class: type) -> list[tuple[str, object, bool]]:
    """Key in configuration file, type and whether it's required, for each field"""
    if issubclass(config_class, msgspec.Struct):
        return [(field.encode_name, field.type, field.required) for field in msgspec.structs.fields(config_class)]
    if not dataclasses.is_dataclass(config_class):
        return []

    hints = get_type_hints(config_class)
    return [
        (
            field.name,
            hints[field.name],
            field.default is dataclasses.MISSING and field.default_factory is dataclasses.MISSING,
        )
        for field in dataclasses.fields(config_class)
    ]


def fill_sections(config_class: type, data: Any) -> Any:  # noqa: ANN401
    """Add missing required sections as empty, recursively, so the defaults of their fields are used

    Sections with a default (e.g. default_factory) are left out, so their own default is used.
    Data that isn't a dict is returned as is, for msgspec to report it.
    """
    if not isinstance(data, dict) or not is_section(config_class):
        return data

    for key, hint, required in _fields(config_class):
        if not is_section(hint):
            continue
        if isinstance(data.get(key), dict):
            fill_sections(hint, data[key])
        elif key not in data and required:
            data[key] = fill_sections(hint, {})

    return data
