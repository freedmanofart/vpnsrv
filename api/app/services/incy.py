from __future__ import annotations

from urllib.parse import quote


def build_incy_import_link(value: str) -> str:
    """Build an INCY import deep link for a subscription URL or server URI."""

    # Keep the inner URL readable, but escape URL-fragment delimiters and spaces
    # so that the outer custom-scheme URL is not truncated by a browser or OS.
    encoded = quote(value, safe=":/?@&=,+-._~%")
    return f"incy://import/{encoded}"
