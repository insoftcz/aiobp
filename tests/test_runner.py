"""Test runner() in a subprocess, it owns the event loop and signal handlers"""

import signal
import subprocess
import sys
import textwrap
import time

import pytest

PRELUDE = """
import asyncio
from aiobp import on_shutdown, runner

def say(text):
    print(text, flush=True)

async def before():
    say("before tasks cancel")

async def after():
    say("after tasks cancel")
"""


def start(body: str) -> subprocess.Popen[str]:
    code = PRELUDE + textwrap.dedent(body)
    return subprocess.Popen(  # noqa: S603 - our own interpreter and code
        [sys.executable, "-W", "error::DeprecationWarning", "-c", code],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def finish(process: subprocess.Popen[str], timeout: float = 10) -> tuple[list[str], str]:
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()  # SIGTERM would only start another graceful shutdown
        process.communicate()
        raise
    assert process.returncode == 0, stderr
    return stdout.splitlines(), stderr


def wait_for_line(process: subprocess.Popen[str], expected: str) -> None:
    assert process.stdout is not None
    line = process.stdout.readline().strip()
    assert line == expected, line


def test_service_finishing_shuts_down_gracefully() -> None:
    process = start("""
        async def main():
            on_shutdown(after, after_tasks_cancel=True)
            on_shutdown(before)
            say("service ran")

        runner("test", "1.0", main(), endless=False)
        say("exited")
    """)
    lines, stderr = finish(process)
    assert lines == ["service ran", "before tasks cancel", "after tasks cancel", "exited"]
    assert "DeprecationWarning" not in stderr


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_signal_shuts_down_gracefully(sig: signal.Signals) -> None:
    process = start("""
        async def worker():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                say("worker cancelled")
                raise

        async def main():
            on_shutdown(after, after_tasks_cancel=True)
            on_shutdown(before)
            asyncio.create_task(worker())
            await asyncio.sleep(0)
            say("ready")

        runner("test", "1.0", main())
        say("exited")
    """)
    wait_for_line(process, "ready")
    time.sleep(0.1)  # let the runner install its signal handlers
    process.send_signal(sig)
    lines, _ = finish(process)
    assert lines == ["before tasks cancel", "worker cancelled", "after tasks cancel", "exited"]


def test_service_exception_is_logged_and_shuts_down() -> None:
    process = start("""
        async def main():
            on_shutdown(before)
            raise ValueError("boom")

        runner("test", "1.0", main(), endless=False)
        say("exited")
    """)
    lines, stderr = finish(process)
    assert lines == ["before tasks cancel", "exited"]
    assert "ValueError: boom" in stderr


def test_task_ignoring_cancellation_doesnt_block_exit() -> None:
    process = start("""
        async def stubborn():
            while True:
                try:
                    await asyncio.sleep(10)
                except asyncio.CancelledError:
                    say("ignoring cancel")

        async def main():
            asyncio.create_task(stubborn())
            await asyncio.sleep(0)

        runner("test", "1.0", main(), endless=False, shutdown_timeout=0.5)
        say("exited")
    """)
    started = time.monotonic()
    lines, _ = finish(process)
    assert lines[-1] == "exited"
    assert "ignoring cancel" in lines
    assert time.monotonic() - started < 5


def test_shutdown_callback_ignoring_cancellation_doesnt_block_exit() -> None:
    process = start("""
        async def stuck():
            while True:
                try:
                    await asyncio.sleep(10)
                except asyncio.CancelledError:
                    say("ignoring cancel")

        async def main():
            on_shutdown(after, after_tasks_cancel=True)
            on_shutdown(stuck)

        runner("test", "1.0", main(), endless=False, shutdown_timeout=0.5)
        say("exited")
    """)
    started = time.monotonic()
    lines, stderr = finish(process)
    assert lines[-1] == "exited"
    assert "after tasks cancel" in lines  # later callbacks still run
    assert "did not finish in timeout" in stderr
    assert time.monotonic() - started < 5
