"""INI like configuration loader"""

from configparser import ConfigParser
from dataclasses import Field, dataclass
from typing import Annotated, Any, Optional, get_origin

from mashumaro.codecs.basic import BasicDecoder


def parse_value(s: str, t: type) -> Any:  # noqa: ANN401
    """Handle lists and bools"""
    if not isinstance(s, str):
        return s

    if t is list:
        return [v.strip() for v in s.split(",")] if isinstance(s, str) else s

    if t is bool:
        return s.lower() in ("1", "true", "yes")

    return s


@classmethod
def ini_parser(cls: Annotated, data: dict[Any, Any]) -> dict[Any, Any]:
    """Value type conversion based on annotations"""
    types = {k: get_origin(v) or v for k, v in cls.__annotations__.items()}
    return {k: parse_value(v, types.get(k)) for k, v in data.items()}


def loader(config_class: type[dataclass], filename: Optional[str] = None) -> Annotated:
    """INI like configuration loader"""
    for section_class in config_class.__dataclass_fields__.values():
        if isinstance(section_class, Field) and section_class.default_factory is not dict:
            section_class.default_factory.__pre_deserialize__ = ini_parser

    config_decoder = BasicDecoder(config_class)

    if filename is None:
        return config_decoder.decode({})

    conf = ConfigParser()
    conf.read(filename)
    config = {section: dict(conf.items(section)) for section in conf.sections()}

    return config_decoder.decode(config)
