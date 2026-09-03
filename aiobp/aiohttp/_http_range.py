"""HTTP Range request support: parse Range headers, serve full or partial content."""

import inspect
from typing import Any, Optional, Protocol, Union

from aiohttp import hdrs, web
from yarl import URL

# Default chunk size used when a Range header omits the end position (e.g. "bytes=0-").
_DEFAULT_CHUNK_SIZE = 32768
# Size of each write() while streaming a file-like source, keeping memory use bounded.
_STREAM_CHUNK_SIZE = 65536


def http_range(http_range_slice: slice) -> Optional[tuple[int, int]]:
    """Parse HTTP range request bytes"""
    range_start = http_range_slice.start
    range_stop = http_range_slice.stop
    if range_start is None:
        return None
    if range_stop is None:
        range_stop = range_start + _DEFAULT_CHUNK_SIZE - 1
    return range_start, range_stop


def range_headers(bytes_range: tuple[int, int], total_length: int) -> dict[str, str]:
    """Create response HTTP range headers"""
    start, end = bytes_range
    if end >= total_length:
        end = total_length - 1
    return {
        "Accept-Ranges": "bytes",
        "Content-Range": f"bytes {start}-{end}/{total_length}",
    }


def _parse_range_header(value: str) -> Optional[slice]:
    """Parse a ``Range: bytes=start-end`` header into a ``slice(start, end)`` of positions."""
    prefix = "bytes="
    if not value.startswith(prefix):
        return None

    range_spec = value[len(prefix):].split(",", 1)[0].strip()
    start_str, sep, stop_str = range_spec.partition("-")
    if not sep or not start_str:
        return None

    try:
        start = int(start_str)
        stop = int(stop_str) if stop_str else None
    except ValueError:
        return None

    return slice(start, stop)


class SeekableAsyncFile(Protocol):
    """Minimal async file interface accepted by ``HttpRangeRequest.stream_response()``.

    ``seek()`` only has to support an absolute offset (``whence=0``); some
    wrappers (e.g. aiofile's ``async_open()``) don't accept ``whence`` at all,
    return ``None`` instead of the new position, and aren't even a coroutine.
    ``stream_response()`` tolerates all of that — it only ever calls
    ``seek(offset)`` with a single argument and ignores the return value.
    """

    def seek(self, offset: int, whence: int = 0) -> Any: ...  # noqa: ANN401

    async def read(self, size: int = -1) -> bytes: ...


class HttpRangeRequest:
    """Resolve a request's ``Range`` header and serve full or partial content.

    Register as a type injector so handlers can request it directly::

        async def download_file(range: HttpRangeRequest):
            # aiofile's seek() can't report the file size, so provide it explicitly.
            # os.path.getsize() blocks, so run it off-thread rather than stalling the loop.
            loop = asyncio.get_running_loop()
            total_length = await loop.run_in_executor(None, os.path.getsize, "some_file.csv")
            async with async_open("some_file.csv") as afp:
                return await range.stream_response(afp, total_length=total_length)

        async def download_api(range: HttpRangeRequest):
            piece, total_size = await api.get("some_key", range.bytes_range)
            return range.chunk_response(piece, total_size)

        def download_cached(range: HttpRangeRequest):
            data = cache.get("key")  # bytes/str already in memory
            return range.slice_response(data)

    ``slice_response()`` and ``chunk_response()`` are synchronous: they never
    suspend, so nothing else can run on the event loop while they build the
    response. ``slice_response()`` takes bytes/str already held in memory and
    slices them to the requested range; ``chunk_response()`` takes data that's
    already been sliced to ``bytes_range`` by the caller (plus the resource's
    total length) and just wraps it with the right status/headers, without
    slicing it again.

    ``stream_response()`` is async because it awaits real I/O: it takes an
    already-open async file-like object (open and close it yourself with
    ``async with``) and writes it out in bounded chunks instead of buffering
    it whole. Use it for anything that could be large. Pass ``total_length``
    explicitly if the object can't report its own size (see
    ``SeekableAsyncFile`` below).

    Set ``response_headers`` before calling any of these: ``stream_response()``
    sends headers as part of ``prepare()``, before it returns, so mutating the
    response's own ``.headers`` afterwards is too late.

    Without a Range header the whole content is returned with status 200;
    with one, only the requested slice is returned with status 206 (or 416
    if it can't be satisfied).

    ``Content-Length`` is always added automatically: ``web.Response`` (used
    by ``slice_response()``/``chunk_response()``) computes it from the
    body/text you pass it, and ``stream_response()`` sets it explicitly
    before ``prepare()`` since a ``StreamResponse`` has no buffered body to
    measure it from.
    """

    __slots__ = ("_range", "_request", "response_headers")

    def __init__(self, request: web.Request) -> None:
        self._request: web.Request = request
        self.response_headers: dict[str, str] = {}
        header = request.headers.get(hdrs.RANGE)
        parsed = _parse_range_header(header) if header else None
        self._range: Optional[tuple[int, int]] = http_range(parsed) if parsed is not None else None

    @property
    def bytes_range(self) -> Optional[tuple[int, int]]:
        """The requested (start, end) byte positions, or None if no Range header was sent."""
        return self._range

    @property
    def url(self) -> URL:
        """The request's URL."""
        return self._request.url

    def slice_response(self, data: Union[bytes, str]) -> web.StreamResponse:
        """Serve bytes/str already held in memory, sliced to the requested range."""
        total_length = len(data)
        if self._range is None:
            return self._to_response(data, status=200, headers=self.response_headers)

        satisfiable = self._clamp(self._range, total_length)
        if satisfiable is None:
            return web.HTTPRequestRangeNotSatisfiable(
                headers={"Content-Range": f"bytes */{total_length}", **self.response_headers},
            )

        start, end = satisfiable
        piece = data[start:end + 1]
        headers = {**range_headers((start, end), total_length), **self.response_headers}
        return self._to_response(piece, status=206, headers=headers)

    def chunk_response(self, data: Union[bytes, str], total_length: int) -> web.StreamResponse:
        """Wrap data that's already sliced to ``bytes_range``, given the resource's total length.

        Use this when the data source (e.g. a cache) does its own range slicing
        given ``bytes_range`` and hands back only the requested portion.
        """
        if self._range is None:
            return self._to_response(data, status=200, headers=self.response_headers)

        satisfiable = self._clamp(self._range, total_length)
        if satisfiable is None:
            return web.HTTPRequestRangeNotSatisfiable(
                headers={"Content-Range": f"bytes */{total_length}", **self.response_headers},
            )

        start, end = satisfiable
        headers = {**range_headers((start, end), total_length), **self.response_headers}
        return self._to_response(data, status=206, headers=headers)

    @staticmethod
    def _to_response(data: Union[bytes, str], *, status: int, headers: dict[str, str]) -> web.StreamResponse:
        if isinstance(data, bytes):
            return web.Response(body=data, status=status, headers=headers)
        return web.Response(text=data, status=status, headers=headers)

    async def stream_response(self, file: SeekableAsyncFile, total_length: Optional[int] = None) -> web.StreamResponse:
        """Serve an already-open file-like source in bounded chunks, without buffering it whole.

        Pass ``total_length`` explicitly when ``file`` doesn't support determining
        its own size via ``seek(0, SEEK_END)`` (e.g. aiofile's ``async_open()``,
        whose ``seek()`` doesn't accept ``whence`` or return the new position).
        """
        if total_length is None:
            try:
                total_length = await self._maybe_await(file.seek(0, 2))
            except TypeError as exc:
                msg = (
                    "Couldn't determine file size via seek(0, SEEK_END) on this "
                    "file-like object — pass total_length explicitly instead."
                )
                raise TypeError(msg) from exc

        if self._range is None:
            return await self._write_stream(file, 0, total_length - 1, status=200, headers=self.response_headers)

        satisfiable = self._clamp(self._range, total_length)
        if satisfiable is None:
            return web.HTTPRequestRangeNotSatisfiable(
                headers={"Content-Range": f"bytes */{total_length}", **self.response_headers},
            )

        start, end = satisfiable
        headers = {**range_headers((start, end), total_length), **self.response_headers}
        return await self._write_stream(file, start, end, status=206, headers=headers)

    async def _write_stream(
        self,
        file: SeekableAsyncFile,
        start: int,
        end: int,
        *,
        status: int,
        headers: dict[str, str],
    ) -> web.StreamResponse:
        response = web.StreamResponse(status=status, headers=headers)
        response.content_length = end - start + 1
        await response.prepare(self._request)

        await self._maybe_await(file.seek(start))
        remaining = end - start + 1
        while remaining > 0:
            chunk = await file.read(min(_STREAM_CHUNK_SIZE, remaining))
            if not chunk:
                break
            await response.write(chunk)
            remaining -= len(chunk)

        await response.write_eof()
        return response

    @staticmethod
    async def _maybe_await(value: Any) -> Any:  # noqa: ANN401
        """Await ``value`` if it's awaitable, otherwise pass it through as-is."""
        return await value if inspect.isawaitable(value) else value

    @staticmethod
    def _clamp(bytes_range: tuple[int, int], total_length: int) -> Optional[tuple[int, int]]:
        """Clamp the requested range to the content length, or None if unsatisfiable."""
        start, end = bytes_range
        if start >= total_length:
            return None
        return start, min(end, total_length - 1)
