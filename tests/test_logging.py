"""Test config loaders"""

import logging
from contextlib import contextmanager

import pytest

from aiobp.logging import suppress_and_log


@contextmanager
def assert_no_logs(level: str = "ERROR"):
    """Context manager that fails if any log at *level* or above is emitted."""
    numeric = getattr(logging, level)
    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = lambda record: records.append(record)  # type: ignore[assignment]
    root = logging.getLogger()
    old_level = root.level
    root.setLevel(min(old_level, numeric))
    root.addHandler(handler)
    try:
        yield
    finally:
        root.removeHandler(handler)
        root.setLevel(old_level)
    errors = [r for r in records if r.levelno >= numeric]
    if errors:
        msgs = [handler.format(r) for r in errors]
        raise AssertionError(f"Unexpected log records at {level}: {msgs}")


def test_surpressed_exception(caplog: pytest.LogCaptureFixture) -> None:
    try:
        with caplog.at_level(logging.ERROR), suppress_and_log(KeyError):
            raise KeyError
    except KeyError:
        raise AssertionError("Exception not surpressed")
    assert any("Suppressed exception" in message for message in caplog.messages)


def _assert_muted(mute: object, caplog: pytest.LogCaptureFixture) -> None:
    """Assert KeyError is suppressed silently and TypeError is suppressed with logging."""
    with assert_no_logs(level="ERROR"):
        try:
            with suppress_and_log(KeyError, TypeError, mute=mute):  # type: ignore[arg-type]
                raise KeyError
        except KeyError:
            raise AssertionError("Exception not suppressed")

    with caplog.at_level(logging.ERROR):
        try:
            with suppress_and_log(KeyError, TypeError, mute=mute):  # type: ignore[arg-type]
                raise TypeError
        except TypeError:
            raise AssertionError("Exception not suppressed")
    assert any("Suppressed exception" in message for message in caplog.messages)


def test_muted_exception_tuple(caplog: pytest.LogCaptureFixture) -> None:
    _assert_muted((KeyError,), caplog)


def test_muted_exception_list(caplog: pytest.LogCaptureFixture) -> None:
    _assert_muted([KeyError], caplog)


def test_muted_exception_single_type(caplog: pytest.LogCaptureFixture) -> None:
    _assert_muted(KeyError, caplog)


def test_not_surpressed_exception() -> None:
    try:
        with suppress_and_log(KeyError):
            raise TypeError
        raise AssertionError("Not listed exception was surpressed")
    except TypeError:
        pass
