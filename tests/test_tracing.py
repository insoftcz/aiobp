"""Trace context helpers that work on plain header mappings"""

import asyncio
import re

import pytest

from aiobp.tracing import (
    context_from_headers,
    new_traceparent,
    propagation_headers,
    setup_tracing,
    trace_id,
    traced,
    use_context,
)

TRACEPARENT = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"


@pytest.fixture(scope="module", autouse=True)
def tracing() -> None:
    setup_tracing("aiobp-tests", "0", None)


def test_setup_without_endpoint_still_creates_trace_ids() -> None:
    async def run() -> str:
        async with traced("root"):
            return trace_id()

    assert re.fullmatch(r"[0-9a-f]{32}", asyncio.run(run()))


def test_context_from_headers_matches_any_case() -> None:
    for key in ("traceparent", "Traceparent", "TRACEPARENT"):
        with use_context(context_from_headers({key: TRACEPARENT})):
            assert propagation_headers()["traceparent"].split("-")[1] == TRACE_ID


def test_context_from_headers_without_traceparent_is_none() -> None:
    assert context_from_headers(None) is None
    assert context_from_headers({}) is None
    assert context_from_headers({"ucs-user": "admin"}) is None


def test_use_context_none_changes_nothing() -> None:
    with use_context(None):
        assert trace_id() == ""


def test_new_traceparent() -> None:
    assert re.fullmatch(r"00-[0-9a-f]{32}-[0-9a-f]{16}-01", new_traceparent())
    assert new_traceparent(TRACE_ID.upper()).split("-")[1] == TRACE_ID  # a bare trace id is kept
    assert new_traceparent("not-a-trace-id").split("-")[1] != "not-a-trace-id"
