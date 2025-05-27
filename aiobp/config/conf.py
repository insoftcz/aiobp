"""INI like configuration loader"""

from configparser import ConfigParser
from dataclasses import dataclass
from typing import Annotated, Any, Optional, get_args, get_origin, get_type_hints

import msgspec


def parse_value(s: str, t: type) -> Any:  # noqa: ANN401
    """Handle lists and bools"""
    if not isinstance(s, str):
        return s

    if t is list:
        return [v.strip() for v in s.split(",")] if isinstance(s, str) else s

    if t is bool:
        return s.lower() in ("1", "true", "yes")

    return s


def __retype(val: Any, composite_type: Optional[type]) -> Any:
    """Retype value to it's annotation"""
    if composite_type is None:
        return val

    basic_type = get_origin(composite_type) or composite_type
    args = get_args(composite_type)

    if basic_type in (int, float):
        return basic_type(val)

    if basic_type is bool:
        return val in ("1", "true", "yes")

    if basic_type in (list, tuple, set):
        if args:
            val_type = args[0]
            return basic_type([val_type(item.strip()) for item in val.split(",")])

        return basic_type([item.strip() for item in val.split(",")])

    if not isinstance(val, dict):
        return val

    if args and set(args).issubset((str, int, float, bool)):
        # this is whole ini section returned as dict
        key_type, val_type = args
        return {key_type(k): val_type(v) for k, v in val.items()}

    return __ini_typer(composite_type, val)


def __ini_typer(obj: Annotated, data: dict[Any, Any]) -> dict[Any, Any]:
    """Value type conversion based on annotations"""
    hints = get_type_hints(obj)
    return {key: __retype(val, hints.get(key)) for key, val in data.items()}


def loader(config_class: type[dataclass], filename: Optional[str] = None) -> Annotated:
    """INI like configuration loader"""
    if filename is None:
        return msgspec.convert({}, type=config_class)

    conf = ConfigParser()
    conf.read(filename)
    config = {section: dict(conf.items(section)) for section in conf.sections()}
    return msgspec.convert(__ini_typer(config_class, config), type=config_class)
