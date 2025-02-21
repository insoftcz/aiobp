"""Add dev and trace logging shortcuts"""

import functools
import logging

dev = functools.partial(logging.log, 1)
debug = logging.debug
info = logging.info
warning = logging.warning
error = logging.error
critical = logging.critical
exception = logging.exception
trace = logging.exception
