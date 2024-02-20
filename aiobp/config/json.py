"""Load configuration from JSON file"""

import json

from typing import Annotated, Optional, Type

from .exceptions import InvalidConfigFile
from .annotations import get_options, set_options


def loader(config_class: Type[Annotated], filename: Optional[str] = None) -> Annotated:
    """Load configuration from JSON file"""
    config = get_options(config_class)

    if filename is None:
        return set_options(config_class(), config)

    with open(filename, 'r', encoding='ascii') as fp:
        conf = json.load(fp)

    if not isinstance(conf, dict):
        # TODO: add unlimited depth support
        raise InvalidConfigFile('JSON configuration structure must be dict[str, dict[str, any]]')

    for module_name, options in config.items():
        module_conf = config.get(module_name, {})
        for option_name, (option_type, default_value) in options.items():
            try:
                value = option_type(module_conf[option_name])
            except KeyError:
                value = default_value
            except Exception as error:
                value = error

            options[option_name] = value

    return set_options(config_class(), config)
