"""Load configuration from YAML file"""

from pathlib import Path
from typing import Annotated, Optional, get_origin

import yaml

from .annotations import ConfigOption, get_options, parse_list, set_options
from .exceptions import InvalidConfigFile, InvalidConfigImplementation


def loader(config_class: type[Annotated], filename: Optional[str] = None) -> Annotated:
    """Load configuration from YAML file"""
    config = get_options(config_class)

    if filename is None:
        return set_options(config_class(), config)

    file = Path(filename)
    with file.open(encoding="utf-8") as fp:
        conf = yaml.safe_load(fp)

    if not isinstance(conf, dict):
        error = "YAML configuration structure must be dict[str, dict[str, any]]"
        raise InvalidConfigFile(error)

    for section_name, options in config.items():
        if not isinstance(options, dict):
            error = f'Class "{config_class.__name__}" can\'t have direct option "{section_name}"'
            raise InvalidConfigImplementation(error)

        conf_section = conf.get(section_name, {})

        for option_name, option in options.items():
            if not isinstance(option, ConfigOption):
                error = f'"{section_name}" can have only scalar attributes, not subsection "{option_name}"'
                raise InvalidConfigImplementation(error)

            value = conf_section.get(option_name, option.value)
            if isinstance(value, Exception):
                options[option_name] = value
            else:
                typed_list = get_origin(option.type) is list
                options[option_name] = parse_list(option.type, value) if typed_list else option.type(value)

    return set_options(config_class(), config)
