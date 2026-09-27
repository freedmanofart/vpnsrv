from __future__ import annotations

from urllib.parse import quote


def build_freevpn_import_link(value: str) -> str:
    """Build a Freedom VPN import deep link for a subscription URL."""

    # Keep the embedded HTTPS URL readable while escaping spaces and characters
    # that could otherwise terminate the outer custom-scheme URL.
    encoded = quote(value, safe=":/?@&=,+-._~%")
    return f"freevpn://import/{encoded}"
