"""Configuration structure from class annotations

Given config:

class ServiceConfig:
    host: str
    port: int = 8080


class LogConfig:
    level: str = "DEBUG"
    filename: str


class Config:
    service: ServiceConfig
    log: LogConfig


Method get_options inspects classes recursively and return configuration map as
nested dicts of options/attributes (keys) having type and default value (vals):

config_map = get_options(Config)
{
    # module name
    "service": {
        # option name: type, default value or AttributeError if default is not provided
        "host": (str, AttributeError),
        "port": (int, 8080),
    },
    "log": {
        "level": (str, "DEBUG"),
        "filename": (str, AttributeError),
    },
}


Loader class constructor will use configuration map, fill values from configuration
file (override default values) and use set_options method to make instances of nested
configuration classes:


class Loader:
    def __init__(self, filename):
        config_map = get_options(Config)

        # parse filename and fill values

        set_options(self, config_map)


Finally what we need to use in our microservice:

config = Config(Loader('some.file'))
"""

from collections import namedtuple
from typing import Annotated, Optional, Type, Union, get_type_hints

from .exceptions import InvalidConfigFile, InvalidConfigImplementation


ConfigOption = namedtuple('Option', ['type', 'value'])


ConfigMap = dict[str, Union['ConfigMap', ConfigOption]]


CONFIG_TYPES = (
    bool,
    float,
    int,
    str,
    Optional[bool],
    Optional[float],
    Optional[int],
    Optional[str],
)


def get_options(config_class: Type[Annotated]) -> ConfigMap:
    """Extract configuration map recursively"""
    config: ConfigMap = {}

    options = get_type_hints(config_class)
    if not options:
        raise InvalidConfigImplementation(f'Configuration class "{config_class}" has no configuration options')

    for option_name, option_type in options.items():
        if option_type not in CONFIG_TYPES:
            config[option_name] = get_options(option_type)
            continue

        try:
            default_value = getattr(config_class, option_name)
        except AttributeError as error:
            default_value = error
        config[option_name] = ConfigOption(option_type, default_value)

    return config


def set_options(config: Annotated, values: ConfigMap, path: Optional[list[str]] = None) -> Annotated:
    """Make instances of configuration classes and set values"""
    for option_name, option_type in get_type_hints(config).items():
        if option_type not in CONFIG_TYPES:
            path = [option_name] if path is None else [*path, option_name]
            value = set_options(option_type(), values.get(option_name), path)
        else:
            value = values.get(option_name)
            if isinstance(value, Exception):
                raise InvalidConfigFile(f'missing option "{option_name}" in section "{".".join(path)}"') from value

        setattr(config, option_name, value)

    return config
