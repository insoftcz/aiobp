"""Content-Type/Content-Disposition headers for file responses"""

import re
from typing import Optional
from urllib.parse import quote

# RFC 7230 field-value grammar only allows VCHAR / obs-text / SP / HTAB — strip everything
# else (not just CR/LF) so a filename can never smuggle control characters into the header.
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]+")
_EXTRA_SPACES = re.compile(r" {2,}")


def content_disposition(disposition: str, filename: str) -> str:
    """Build a Content-Disposition header value that is safe for arbitrary filenames

    HTTP header values may not contain raw control characters such as CR/LF - that's not
    a quoting issue, it's outright forbidden (RFC 7230), since allowing it would enable
    header/response-splitting attacks. The plain `filename` fallback therefore has those
    characters collapsed to a single space. If that changed anything, `filename*` (RFC 6266)
    is added with the full name percent-encoded so clients that support it still see the
    original filename; it's skipped when the fallback already matches it exactly.
    """
    ascii_fallback = _CONTROL_CHARS.sub(" ", filename).replace("\\", " ").replace('"', " ")
    ascii_fallback = _EXTRA_SPACES.sub(" ", ascii_fallback).strip()
    header = f'{disposition}; filename="{ascii_fallback}"'
    if ascii_fallback != filename:
        encoded = quote(filename, safe="")
        header += f"; filename*=UTF-8''{encoded}"
    return header


def file_headers(content_type: str, filename: Optional[str] = None, *, download: bool = True) -> dict[str, str]:
    """Build Content-Type/Content-Disposition headers for a file response

    With a `filename`, adds an RFC 6266 Content-Disposition header ("attachment" unless
    `download` is False, in which case "inline" so the browser displays the file in place).
    Without a filename there is nothing to dispose of, so text/html content instead gets an
    explicit UTF-8 charset to stop browsers from guessing one when rendering the body inline.
    """
    if filename:
        disposition = "attachment" if download else "inline"
        return {
            "Content-Type": content_type,
            "Content-Disposition": content_disposition(disposition, filename),
        }

    if content_type == "text/html":
        content_type += "; charset=utf-8"
    return {"Content-Type": content_type}
