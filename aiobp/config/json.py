"""Load configuration from JSON file"""

from pathlib import Path
from typing import Annotated, Optional

from mashumaro.codecs.json import JSONDecoder


def loader(config_class: type[Annotated], filename: Optional[str] = None) -> Annotated:
    """Load configuration from JSON file"""
    config_decoder = JSONDecoder(config_class)

    if filename is None:
        return config_decoder.decode("{}")

    config = Path(filename).read_bytes()
    return config_decoder.decode(config)
