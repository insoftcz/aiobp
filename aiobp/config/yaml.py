"""Load configuration from YAML file"""

from pathlib import Path
from typing import Annotated, Optional

from mashumaro.codecs.yaml import YAMLDecoder


def loader(config_class: type[Annotated], filename: Optional[str] = None) -> Annotated:
    """Load configuration from YAML file"""
    config_decoder = YAMLDecoder(config_class)

    if filename is None:
        return config_decoder.decode("{}")

    config = Path(filename).read_bytes()
    return config_decoder.decode(config)
