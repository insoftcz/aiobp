"""INI like configuration loader"""

import configparser
from typing import Annotated, Optional, get_origin

from .annotations import ConfigOption, get_options, parse_list, set_options
from .exceptions import InvalidConfigImplementation


def loader(config_class: type[Annotated], filename: Optional[str] = None) -> Annotated:
    """INI like configuration loader"""
    config = get_options(config_class)

    if filename is None:
        return set_options(config_class(), config)

    conf = configparser.ConfigParser()
    conf.read(filename)
    for section_name, options in config.items():
        if not isinstance(options, dict):
            error = f'Class "{config_class.__name__}" can\'t have direct option "{section_name}"'
            raise InvalidConfigImplementation(error)

        for option_name, option in options.items():
            if not isinstance(option, ConfigOption):
                error = f'"{section_name}" can have only scalar attributes, not subsection "{option_name}"'
                raise InvalidConfigImplementation(error)

            if option.type is int:
                get = conf.getint
            elif option.type is float:
                get = conf.getfloat
            elif option.type is bool:
                get = conf.getboolean
            else:
                get = conf.get

            value = get(section_name, option_name, fallback=option.value)

            options[option_name] = parse_list(option.type, value) if get_origin(option.type) is list else value

    return set_options(config_class(), config)
