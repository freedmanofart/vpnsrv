import re
from base64 import b64decode
from urllib.parse import parse_qs, quote, urlsplit

import httpx

from app.db.models.vpn_client import VPNClient
from app.db.models.vpn_node import VPNNode
from app.db.models.vpn_node_config import VPNNodeConfig
from app.services.threexui import ThreeXUIClient, ThreeXUIError


_KIT_URI = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*)://\S+$")
_KIT_SCHEMES = {
    "vless", "vmess", "trojan", "ss", "hysteria2", "hy2", "tuic",
    "wireguard", "amneziawg", "vpn", "socks", "socks5", "http",
}


def _subscription_text(body: bytes) -> str:
    text = body.decode("utf-8-sig", errors="ignore").strip()
    if "://" not in text:
        try:
            padded = text.encode("ascii") + b"=" * (-len(text) % 4)
            decoded = b64decode(padded, altchars=b"-_", validate=True).decode(
                "utf-8-sig", errors="ignore"
            )
            if "://" in decoded:
                text = decoded
        except (UnicodeEncodeError, ValueError):
            pass
    return text


def kit_subscription_uris(body: bytes) -> list[str]:
    uris = []
    for line in _subscription_text(body).splitlines():
        candidate = line.strip()
        match = _KIT_URI.match(candidate)
        if match and match.group(1).lower() in _KIT_SCHEMES:
            uris.append(candidate)
    return uris


def kit_telegram_proxy_uri(body: bytes) -> str | None:
    for line in _subscription_text(body).splitlines():
        candidate = line.strip()
        try:
            parsed = urlsplit(candidate)
            query = parse_qs(parsed.query, strict_parsing=True)
            port_values = query.get("port", [])
            port = int(port_values[0]) if len(port_values) == 1 else 0
        except (TypeError, ValueError):
            continue
        if (
            parsed.scheme.lower() == "tg"
            and parsed.netloc.lower() == "proxy"
            and len(query.get("server", [])) == 1
            and bool(query["server"][0])
            and 1 <= port <= 65535
            and len(query.get("secret", [])) == 1
            and bool(query["secret"][0])
        ):
            return candidate
    return None


async def kit_subscription_target(
    client: VPNClient,
    node: VPNNode,
    node_config: VPNNodeConfig,
) -> tuple[str, str] | None:
    try:
        enabled_clients = node_config.config.get("kit_subscription_client_ids", [])
        enabled_for_all = node_config.config.get("kit_subscription_all_clients") is True
        if not enabled_for_all and (
            not isinstance(enabled_clients, list) or client.id not in enabled_clients
        ):
            return None
        base = str(node_config.config.get("kit_subscription_base_url") or "").strip()
        parsed = urlsplit(base)
        parsed_port = parsed.port
    except (AttributeError, TypeError, ValueError):
        return None
    allowed_hosts = {node.ip_address.lower()}
    if node.hostname:
        allowed_hosts.add(node.hostname.lower())
    if (
        client.protocol != "vless"
        or parsed.scheme != "https"
        or (parsed.hostname or "").lower() not in allowed_hosts
        or parsed_port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not re.fullmatch(r"/[A-Za-z0-9_-]{1,64}/", parsed.path)
    ):
        return None
    try:
        panel = ThreeXUIClient(node_config.config.get("api_address"), timeout=8)
        info = await panel.get_client_subscription_info(f"vpn-{client.id}")
        sub_id = info["sub_id"]
        if not sub_id or not re.fullmatch(r"[A-Za-z0-9_.@-]{1,64}", sub_id):
            return None
        if len(info["inbound_ids"]) < 2:
            return None
        return base, sub_id
    except (ThreeXUIError, KeyError, TypeError, ValueError):
        return None


async def fetch_kit_subscription(
    client: VPNClient,
    node: VPNNode,
    node_config: VPNNodeConfig,
    user_agent: str = "",
    accept: str = "",
) -> tuple[bytes, httpx.Headers] | None:
    try:
        target = await kit_subscription_target(client, node, node_config)
        if target is None:
            return None
        base, sub_id = target
        async with httpx.AsyncClient(timeout=15, follow_redirects=False) as session:
            response = await session.get(
                f"{base.rstrip('/')}/{quote(sub_id, safe='._@-')}",
                headers={
                    "User-Agent": (user_agent or "Freedom VPN")[:200],
                    "Accept": accept[:256] or "*/*",
                },
            )
        if response.status_code != 200 or len(response.content) > 2 * 1024 * 1024:
            return None
        return response.content, response.headers
    except (ThreeXUIError, httpx.HTTPError, KeyError, TypeError, ValueError):
        return None


async def kit_subscription_for_client(
    client: VPNClient,
    node: VPNNode,
    node_config: VPNNodeConfig,
    user_agent: str,
    accept: str = "",
) -> dict | None:
    """Fetch app-compatible KIT profiles only when this user has multiple inbounds."""
    fetched = await fetch_kit_subscription(client, node, node_config, user_agent, accept)
    if fetched is None:
        return None
    body, headers = fetched
    uris = kit_subscription_uris(body)
    if not uris:
        return None
    return {
        "body": body,
        "content_type": "text/plain",
        "headers": headers,
        "uris": uris,
    }


async def kit_telegram_proxy_for_client(
    client: VPNClient,
    node: VPNNode,
    node_config: VPNNodeConfig,
) -> str | None:
    fetched = await fetch_kit_subscription(
        client,
        node,
        node_config,
        user_agent="Freedom VPN payment notification",
        accept="text/plain,*/*",
    )
    return kit_telegram_proxy_uri(fetched[0]) if fetched else None
