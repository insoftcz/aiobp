"""Test config loaders"""

import logging
import unittest
from contextlib import contextmanager

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


class TestLogging(unittest.TestCase):

    def test_surpressed_exception(self) -> None:
        try:
            with self.assertLogs(level="ERROR") as cm:
                with suppress_and_log(KeyError):
                    raise KeyError
        except KeyError:
            raise AssertionError("Exception not surpressed")
        self.assertTrue(any("Suppressed exception" in msg for msg in cm.output))

    def _assert_muted(self, mute: object) -> None:  # type: ignore[explicit-any]
        """Assert KeyError is suppressed silently and TypeError is suppressed with logging."""
        with assert_no_logs(level="ERROR"):
            try:
                with suppress_and_log(KeyError, TypeError, mute=mute):  # type: ignore[arg-type]
                    raise KeyError
            except KeyError:
                raise AssertionError("Exception not suppressed")

        with self.assertLogs(level="ERROR") as cm:
            try:
                with suppress_and_log(KeyError, TypeError, mute=mute):  # type: ignore[arg-type]
                    raise TypeError
            except TypeError:
                raise AssertionError("Exception not suppressed")
        self.assertTrue(any("Suppressed exception" in msg for msg in cm.output))

    def test_muted_exception_tuple(self) -> None:
        self._assert_muted((KeyError,))

    def test_muted_exception_list(self) -> None:
        self._assert_muted([KeyError])

    def test_muted_exception_single_type(self) -> None:
        self._assert_muted(KeyError)

    def test_not_surpressed_exception(self) -> None:
        try:
            with suppress_and_log(KeyError):
                raise TypeError
            raise AssertionError("Not listed exception was surpressed")
        except TypeError:
            pass


if __name__ == "__main__":
    unittest.main()
