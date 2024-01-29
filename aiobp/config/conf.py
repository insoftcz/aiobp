"""INI like configuration loader"""

import configparser

from typing import Annotated, Type

from .annotations import ConfigOption, get_options, set_options
from .exceptions import InvalidConfigImplementation


def loader(filename: str, config_class: Type[Annotated]) -> Annotated:
    """INI like configuration loader"""
    conf = configparser.ConfigParser()
    conf.read(filename)

    config = get_options(config_class)

    for section_name, options in config.items():
        if not isinstance(options, dict):
            raise InvalidConfigImplementation(f'Class "{config_class.__name__}" can\'t have direct option "{section_name}"')

        for option_name, option in options.items():
            if not isinstance(option, ConfigOption):
                raise InvalidConfigImplementation(f'"{section_name}" can have only scalar attributes, not subsection "{option_name}"')

            if isinstance(option.type, int):
                get = conf.getint
            elif isinstance(option.type, float):
                get = conf.getfloat
            elif isinstance(option.type, bool):
                get = conf.getboolean
            else:
                get = conf.get

            options[option_name] = get(section_name, option_name, fallback=option.value)

    return set_options(config_class(), config)
