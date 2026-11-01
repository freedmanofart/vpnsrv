from __future__ import annotations

from urllib.parse import quote, urlsplit


HAPP_SUPPORTED_PROTOCOLS = frozenset(
    {"vless", "vmess", "trojan", "ss", "socks", "socks5", "hysteria2", "hy2"}
)


def is_happ_compatible_profile(profile: dict) -> bool:
    if not str(profile.get("config") or "").strip():
        return False
    protocol = str(
        profile.get("protocol") or urlsplit(str(profile.get("config") or "")).scheme
    ).lower()
    return protocol in HAPP_SUPPORTED_PROTOCOLS


def build_happ_import_link(subscription_url: str) -> str:
    """Wrap a standard HTTPS subscription in Happ's documented deep link."""
    encoded = quote(subscription_url, safe=":/?@&=,+-._~%")
    return f"happ://add/{encoded}"
