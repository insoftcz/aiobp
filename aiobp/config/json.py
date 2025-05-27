"""Load configuration from JSON file"""

from pathlib import Path
from typing import Annotated, Optional

import msgspec


def loader(config_class: type[Annotated], filename: Optional[str] = None) -> Annotated:
    """Load configuration from JSON file"""
    if filename is None:
        return msgspec.json.decode("{}", type=config_class)

    config = Path(filename).read_bytes()
    return msgspec.json.decode(config, type=config_class)
