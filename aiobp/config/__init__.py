"""Service configuration"""

import os
import sys

from .exceptions import InvalidConfigFile


def sys_argv_or_filenames(*filenames: str) -> str:
    """Return usable configuration filename"""
    if len(sys.argv) == 2:
        filename = sys.argv[1]
        if not os.path.isfile(filename):
            raise InvalidConfigFile(f'Provided filename "{filename}" not found')

        return filename

    for filename in filenames:
        if not os.path.isfile(filename):
            continue

        return filename

    raise InvalidConfigFile(f'None of default filenames found: {", ".join(filenames)}')


__all__ = ["InvalidConfigFile", "sys_argv_or_filenames"]
