"""Customize Python logger"""

import logging
import logging.handlers
import sys
from dataclasses import dataclass
from types import TracebackType
from typing import ClassVar, Optional


@dataclass
class LoggingConfig:
    """Loging configuration"""

    level: str = "DEBUG"
    filename: Optional[str] = None
    otel_endpoint: Optional[str] = None  # e.g. "http://localhost:4317"
    otel_service_name: Optional[str] = None
    otel_logs_interval: int = 5  # seconds, 0 = instant


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

    severity_color: ClassVar[dict[int, str]] = ({
        logging.DEBUG: Color.grey,
        logging.INFO: Color.white,
        logging.WARNING: Color.orange,
        logging.ERROR: Color.red,
        logging.CRITICAL: Color.magenta,
    })

    def format(self, record: logging.LogRecord) -> str:
        """Format message according to its severity level"""
        color = ColorFormatter.severity_color.get(record.levelno, Color.blue)
        record.message = f"{color}{record.getMessage()}{Color.reset}"
        if self.usesTime():
            record.asctime = self.formatTime(record, self.datefmt)
        s = self.formatMessage(record)
        if record.exc_info and not record.exc_text:
            # Cache the traceback text to avoid converting it multiple times
            # (it's constant anyway)
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


def add_devel_log_level() -> None:
    """Register new logging level for development purposes"""
    logging.addLevelName(1, "DEVEL")


def log_unhandled_exception(type_: type[BaseException], value: BaseException, traceback: TracebackType) -> None:
    """Log unhandled exceptions"""
    if issubclass(type_, KeyboardInterrupt):
        sys.__excepthook__(type_, value, traceback)
        return

    logging.critical("Uncaught exception", exc_info=(type_, value, traceback))

def _setup_otel_logging(
    endpoint: str,
    service_name: str,
    level: str,
    interval: int = 5,
) -> Optional[logging.Handler]:
    """Setup OpenTelemetry logging handler"""
    try:
        from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
        from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor, SimpleLogRecordProcessor
        from opentelemetry.sdk.resources import Resource, SERVICE_NAME
    except ImportError:
        logging.warning("OpenTelemetry packages not installed, OTEL logging disabled")
        return None

    resource = Resource.create({SERVICE_NAME: service_name})
    logger_provider = LoggerProvider(resource=resource)

    exporter = OTLPLogExporter(endpoint=endpoint, insecure=True)

    if interval <= 0:
        processor = SimpleLogRecordProcessor(exporter)
    else:
        processor = BatchLogRecordProcessor(exporter, schedule_delay_millis=interval * 1000)

    logger_provider.add_log_record_processor(processor)

    handler = LoggingHandler(level=logging.getLevelName(level), logger_provider=logger_provider)
    return handler


def setup_logging(config: Optional[LoggingConfig] = None) -> None:
    """Setups Python logger"""
    if config is None:
        config = LoggingConfig()

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(
        ColorFormatter(f"%(asctime)s [{Color.green}%(filename)s:%(lineno)d{Color.reset}] %(message)s"),
    )
    handlers = [console_handler]

    if config and config.filename is not None:
        file_handler = logging.handlers.WatchedFileHandler(filename=config.filename)
        file_handler.setFormatter(
            PrefixExceptionFormatter("%(asctime)s %(levelname)s [%(filename)s:%(lineno)d] %(message)s"),
        )
        handlers.append(file_handler)

    if config and config.otel_endpoint is not None:
        service_name = config.otel_service_name or "unknown-service"
        interval = config.otel_logs_interval if hasattr(config, 'otel_logs_interval') else 5
        otel_handler = _setup_otel_logging(config.otel_endpoint, service_name, config.level, interval)
        if otel_handler:
            handlers.append(otel_handler)

    logging.basicConfig(level=config.level if config else None, handlers=handlers)

    sys.excepthook = log_unhandled_exception
