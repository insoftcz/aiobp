"""Configuration provider"""

import sys
from pathlib import Path

from .exceptions import InvalidConfigFile


def sys_argv_or_filenames(*filenames: str) -> str:
    """Return usable configuration filename"""
    if len(sys.argv) > 1:
        filename = sys.argv[1]
        if not Path(filename).is_file():
            error = f'Provided filename "{filename}" not found'
            raise InvalidConfigFile(error)

        return filename

    for filename in filenames:
        if not Path(filename).is_file():
            continue

        return filename

    error = f'None of default filenames found: {", ".join(filenames)}'
    raise InvalidConfigFile(error)


__all__ = ["InvalidConfigFile", "sys_argv_or_filenames"]
