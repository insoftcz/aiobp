"""Add dev and trace logging shortcuts"""

import functools
import logging


dev = functools.partial(logging.log, 1)
debug = logging.debug
info = logging.info
warning = logging.warning
error = logging.error
critical = logging.critical
trace = functools.partial(logging.log, logging.ERROR, exc_info=True)
