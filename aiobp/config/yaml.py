"""Load configuration from YAML file"""

from pathlib import Path
from typing import Annotated

import msgspec

from aiobp.config.sections import fill_sections


def loader(config_class: type[Annotated], filename: str | Path | None = None) -> Annotated:
    """Load configuration from YAML file, defaults are used without filename and for missing sections"""
    config = {} if filename is None else msgspec.yaml.decode(Path(filename).read_bytes())
    # empty file decodes to None
    return msgspec.convert(fill_sections(config_class, config or {}), type=config_class)
