"""Customize Python logger"""

import logging
import logging.handlers
import sys

from typing import Optional


class LoggingConfig:
    """Loging configuration"""

    level: str = 'DEBUG'
    filename: Optional[str] = None


class Color:
    """Console colors"""

    black = "\x1b[0;30m"
    brown = "\x1b[0;31m"
    dark_green = "\x1b[0;32m"
    orange = "\x1b[0;33m"
    dark_blue = "\x1b[0;34m"
    purple = "\x1b[0;35m"
    teal = "\x1b[0;36m"
    grey = "\x1b[0;37m"
    red = "\x1b[0;91m"
    green = "\x1b[0;92m"
    yellow = "\x1b[0;93m"
    blue = "\x1b[0;94m"
    magenta = "\x1b[0;95m"
    cyan = "\x1b[0;96m"
    white = "\x1b[0;97m"
    reset = "\x1b[0m"


class PrefixExceptionFormatter(logging.Formatter):
    """Add date in front of each line of multiline exception/stack trace"""

    def _prefix_text(self, prefix: str, stack: str, color: Color = Color.brown) -> str:
        """Prefix lines with date so we can easily grep by date and time"""
        return "\n".join([f"{prefix} {color}{line}{Color.reset}" for line in stack.split("\n")][1:])


class ColorFormatter(PrefixExceptionFormatter):
    """Color formatter for console logging"""

    severity_color = {
        logging.DEBUG: Color.grey,
        logging.INFO: Color.white,
        logging.WARNING: Color.orange,
        logging.ERROR: Color.red,
        logging.CRITICAL: Color.magenta,
    }

    def format(self, record: logging.LogRecord) -> str:
        """Format message according to its severity level"""
        color = ColorFormatter.severity_color.get(record.levelno, Color.blue)
        record.message = f"{color}{record.getMessage()}{Color.reset}"
        if self.usesTime():
            record.asctime = self.formatTime(record, self.datefmt)
        s = self.formatMessage(record)
        if record.exc_info:
            # Cache the traceback text to avoid converting it multiple times
            # (it's constant anyway)
            if not record.exc_text:
                record.exc_text = self.formatException(record.exc_info)
        if record.exc_text:
            if s[-1:] != "\n":
                s = s + "\n"
            s = s + self._prefix_text(record.asctime, record.exc_text)
        if record.stack_info:
            if s[-1:] != "\n":
                s = s + "\n"
            s = s + self._prefix_text(record.asctime, self.formatStack(record.stack_info))
        return s


def add_devel_log_level():
    """Register new logging level for development purposes"""
    logging.addLevelName(1, "DEVEL")


def setup_logging(config: Optional[LoggingConfig] = LoggingConfig()) -> None:
    """Setup Python logger"""
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(
        ColorFormatter(f"%(asctime)s [{Color.green}%(filename)s:%(lineno)d{Color.reset}] %(message)s"),
    )
    handlers = [console_handler]

    if config.filename is not None:
        file_handler = logging.handlers.WatchedFileHandler(filename=config.filename)
        file_handler.setFormatter(
            PrefixExceptionFormatter("%(asctime)s %(levelname)s [%(filename)s:%(lineno)d] %(message)s"),
        )
        handlers.append(file_handler)

    logging.basicConfig(level=config.level, handlers=handlers)
