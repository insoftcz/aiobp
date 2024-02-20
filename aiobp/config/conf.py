"""INI like configuration loader"""

import configparser

from typing import Annotated, Optional, Type

from .annotations import ConfigOption, get_options, set_options
from .exceptions import InvalidConfigImplementation


def loader(config_class: Type[Annotated], filename: Optional[str] = None) -> Annotated:
    """INI like configuration loader"""
    config = get_options(config_class)

    if filename is None:
        return set_options(config_class(), config)

    conf = configparser.ConfigParser()
    conf.read(filename)
    for section_name, options in config.items():
        if not isinstance(options, dict):
            raise InvalidConfigImplementation(f'Class "{config_class.__name__}" can\'t have direct option "{section_name}"')

        for option_name, option in options.items():
            if not isinstance(option, ConfigOption):
                raise InvalidConfigImplementation(f'"{section_name}" can have only scalar attributes, not subsection "{option_name}"')

            if option.type is int:
                get = conf.getint
            elif option.type is float:
                get = conf.getfloat
            elif option.type is bool:
                get = conf.getboolean
            else:
                get = conf.get

            options[option_name] = get(section_name, option_name, fallback=option.value)

    return set_options(config_class(), config)
