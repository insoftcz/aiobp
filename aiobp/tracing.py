"""OpenTelemetry tracing — call setup_tracing() once at startup, use traced() / current_span() anywhere."""

import logging
import os
import socket
import traceback
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional, Tuple, Type

from ._otel import endpoint_reachable

log = logging.getLogger(__name__)

try:
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    from opentelemetry.propagate import extract, inject
    from opentelemetry.sdk.resources import HOST_NAME, SERVICE_NAME, SERVICE_VERSION, Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.trace import Status, StatusCode
    _OTEL = True
except Exception as _e:  # broken installs raise more than ImportError (e.g. StopIteration from entry-point lookup)
    _OTEL = False
    _OTEL_IMPORT_ERROR = _e

_tracer = None  # opentelemetry.trace.Tracer when set


def setup_tracing(service_name: str, service_version: str, endpoint: Optional[str]) -> None:
    """Configure OpenTelemetry tracing. Call once at startup.

    Independent of ``setup_logging`` — works alone, or alongside it sharing
    the same OTLP endpoint:

        setup_logging("my-service", config.log)
        setup_tracing("my-service", __version__, config.log.otel_endpoint)

    If ``endpoint`` is falsy or OpenTelemetry packages are not installed,
    tracing is disabled and ``traced()`` becomes a no-op.
    """
    global _tracer
    if not endpoint:
        log.info("OTEL tracing disabled (no endpoint configured)")
        return
    if not _OTEL:
        log.warning("OpenTelemetry unavailable (%s: %s), tracing disabled",
                    type(_OTEL_IMPORT_ERROR).__name__, _OTEL_IMPORT_ERROR)
        return
    if not endpoint_reachable(endpoint):
        log.error("OTEL endpoint %s unreachable, tracing disabled", endpoint)
        return

    resource = Resource.create({
        SERVICE_NAME: service_name,
        SERVICE_VERSION: service_version,
        HOST_NAME: socket.gethostname(),
    })
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, insecure=True)))
    trace.set_tracer_provider(provider)
    _tracer = trace.get_tracer(service_name)
    log.info("OTEL tracing enabled: %s", endpoint)


def current_span():
    """Return the active span, or a no-op span if none."""
    if not _OTEL:
        return _NoopSpan()
    return trace.get_current_span()


def start_span(
    name: str,
    attrs: Optional[Dict[str, Any]] = None,
    context: Optional[Any] = None,
    traceparent: Optional[str] = None,
):
    """Start a span whose lifetime is managed manually — call ``.end()`` on it later.

    Use for spans that outlive a single function call (e.g. waiting for an
    external event to complete). The returned span is NOT installed as the
    current context, so child spans won't auto-nest under it.

    Returns a no-op span if tracing isn't configured.
    """
    if _tracer is None:
        return _NoopSpan()
    if context is None and traceparent:
        context = extract({"traceparent": traceparent})
    return _tracer.start_span(name, context=context, attributes=attrs)


def current_traceparent() -> str:
    """W3C traceparent string for the current active span, or empty if none."""
    return propagation_headers().get("traceparent", "")


def propagation_headers() -> Dict[str, str]:
    """Headers dict carrying the current span context across boundaries."""
    if not _OTEL:
        return {}
    carrier: Dict[str, str] = {}
    inject(carrier)
    return carrier


@asynccontextmanager
async def traced(
    name: str,
    attrs: Optional[Dict[str, Any]] = None,
    context: Optional[Any] = None,
    traceparent: Optional[str] = None,
    suppress: Tuple[Type[BaseException], ...] = (),
    errors_only: bool = False,
):
    """Open a span, attach attrs, stamp ERROR on exception. Suppress types listed in ``suppress``.

    Parent context can be given as either ``context`` (an OTel Context) or
    ``traceparent`` (W3C string). Explicit ``context`` wins if both are
    provided; if neither, the current active context is inherited.

    When ``errors_only=True`` the span is created lazily — only if an
    exception is raised. The normal path produces no span (useful for noisy
    handlers where you only want to surface failures).

    If ``setup_tracing()`` was not called (or OTel packages aren't installed),
    this is a no-op that still propagates exceptions correctly.
    """
    if _tracer is None:
        try:
            yield _NoopSpan()
        except Exception as e:
            if isinstance(e, suppress):
                _log_suppressed(name, e)
            else:
                raise
        return

    if context is None and traceparent:
        context = extract({"traceparent": traceparent})

    if not errors_only:
        with _tracer.start_as_current_span(name, context=context) as span:
            if attrs:
                span.set_attributes(attrs)
            try:
                yield span
            except Exception as e:
                if span.is_recording():
                    span.set_status(Status(StatusCode.ERROR, str(e)))
                    span.record_exception(e)
                if isinstance(e, suppress):
                    _log_suppressed(name, e)
                else:
                    raise
        return

    try:
        yield None
    except Exception as e:
        with _tracer.start_as_current_span(name, context=context) as span:
            if attrs:
                span.set_attributes(attrs)
            span.set_status(Status(StatusCode.ERROR, str(e)))
            span.record_exception(e)
            if isinstance(e, suppress):
                _log_suppressed(name, e)
            else:
                raise


class _NoopSpan:
    """Stand-in span used when tracing isn't configured or OTel isn't installed."""

    def set_attribute(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def set_attributes(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def set_status(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def record_exception(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def add_event(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def is_recording(self) -> bool:
        return False

    def end(self) -> None:
        pass


def _log_suppressed(name: str, e: BaseException) -> None:
    frames = traceback.extract_tb(e.__traceback__)
    if frames:
        f = frames[-1]
        log.error("%s [%s:%d]: %s", name, os.path.basename(f.filename), f.lineno, e)
    else:
        log.error("%s: %s", name, e)
