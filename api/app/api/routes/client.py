import secrets
import binascii
import html
import json
import re
from base64 import b64decode, b64encode, urlsafe_b64encode
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from io import BytesIO
from urllib.parse import parse_qs, quote, unquote, urlsplit
from uuid import UUID, uuid4

import httpx
import qrcode
import yaml
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import require_api_access
from app.core.tokens import generate_scoped_token, token_hash
from app.db.models.audit import ActivationCode, ClientDevice
from app.db.models.plan import Plan
from app.db.models.subscription import Subscription
from app.db.models.user import User
from app.db.models.vpn_client import VPNClient
from app.db.models.vpn_node import VPNNode
from app.db.models.vpn_node_config import VPNNodeConfig
from app.db.session import get_db
from app.schemas.client import (
    ActivationCodeCreate,
    ActivationCodeResponse,
    ClientProfileNode,
    ClientProfileLink,
    ClientProviderInfo,
    ClientProfileUsage,
    ClientProfileResponse,
    DeviceActivate,
    DeviceTokenResponse,
    SubscriptionImport,
    TrialActivate,
)
from app.services.audit import write_audit
from app.services.kit_subscriptions import (
    kit_subscription_for_client,
    kit_subscription_uris,
    kit_subscription_target,
)
from app.services.node_health import node_accepts_clients
from app.services.provider_codes import issue_provider_code
from app.services.threexui import ThreeXUIClient, ThreeXUIError
from app.services.vless import build_vless_url
from app.services.incy import build_incy_import_link
from app.services.freevpn import build_freevpn_import_link
from app.core.config import settings
from app.core.cabinet_links import verify_incy_subscription_token


router = APIRouter(prefix="/v1/client", tags=["Client devices"])
device_bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class DevicePrincipal:
    device_id: int
    user_id: int


def aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def mobile_trial_id(device_id: int) -> str:
    return f"trial-{device_id}"


def incy_metadata_header(value: str) -> str:
    """Encode non-ASCII INCY metadata for HTTP headers."""

    return f"base64:{b64encode(value.encode('utf-8')).decode('ascii')}"


def incy_support_url() -> str:
    """Open the same support flow that is available from the Telegram menu."""

    configured = settings.provider_support_url.strip()
    if configured:
        return configured
    username = settings.bot_username.strip().lstrip("@")
    return f"https://t.me/{username}?start=support" if username else ""


# Freedom VPN's managed-profile importer supports its URL-style VMess parser,
# while 3X-UI KIT exports VMess as legacy base64 JSON. Passing that entry makes
# the client reject the complete provider response as unsupported_service_config.
_FREEDOM_PROFILE_SCHEMES = {"vless", "trojan"}
_INCY_EXCLUDED_SCHEMES = {"wireguard", "amneziawg", "awg", "vpn"}
_INCY_RU_PROFILE_VARIANTS = {
    "Reality",
    "Hysteria2",
    "xHTTP",
    "VLESS WebSocket",
    "Trojan gRPC",
    "VMess WebSocket",
}
_AWG_OPTION_NAMES = {
    "jc": "Jc",
    "jmin": "Jmin",
    "jmax": "Jmax",
    "s1": "S1",
    "s2": "S2",
    "s3": "S3",
    "s4": "S4",
    "h1": "H1",
    "h2": "H2",
    "h3": "H3",
    "h4": "H4",
    "i1": "I1",
    "header-protection-key": "HeaderProtectionKey",
    "content-padding-addition": "ContentPaddingAddition",
    "rekey-after-time": "RekeyAfterTime",
    "rekey-timeout": "RekeyTimeout",
    "reject-after-time": "RejectAfterTime",
    "keepalive-timeout": "KeepaliveTimeout",
    "max-handshake-attempts": "MaxHandshakeAttempts",
}


def clean_profile_name(value: str) -> str:
    """Remove panel quota counters and internal client IDs from a link name."""

    name = unquote(str(value or "")).split("|", 1)[0].strip()
    if re.fullmatch(r"vpn-\d+", name):
        return ""
    return re.sub(r"-vpn-\d+$", "", name).strip(" -")


def normalized_profile_uri(uri: str, index: int, label: str | None = None) -> str:
    """Return an app-facing URI with a stable, human-readable profile name."""

    parsed = urlsplit(uri)
    scheme = parsed.scheme.lower()
    if scheme == "vmess":
        try:
            raw = uri.split("://", 1)[1]
            document = json.loads(b64decode(raw + "=" * (-len(raw) % 4)).decode())
            document["ps"] = label or clean_profile_name(document.get("ps", "")) or "VMess-WS"
            encoded = b64encode(
                json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode()
            ).decode().rstrip("=")
            return f"vmess://{encoded}"
        except (binascii.Error, UnicodeDecodeError, ValueError):
            return uri

    display = label or clean_profile_name(parsed.fragment) or profile_display_name(uri, index)
    return parsed._replace(fragment=quote(display, safe="")).geturl()


def profile_display_name(uri: str, index: int) -> str:
    parsed = urlsplit(uri)
    scheme = parsed.scheme.lower() or "vpn"
    if scheme == "vmess":
        try:
            raw = uri.split("://", 1)[1]
            document = json.loads(b64decode(raw + "=" * (-len(raw) % 4)).decode())
            name = clean_profile_name(document.get("ps", ""))
            if name:
                return name[:80]
        except (binascii.Error, UnicodeDecodeError, ValueError):
            pass
    fragment = clean_profile_name(parsed.fragment)
    if fragment:
        return fragment[:80]
    labels = {
        "vless": "VLESS",
        "vmess": "VMess",
        "trojan": "Trojan",
        "ss": "Shadowsocks",
        "hysteria2": "Hysteria2",
        "hy2": "Hysteria2",
        "tuic": "TUIC",
        "vpn": "AmneziaWG",
        "amneziawg": "AmneziaWG",
    }
    return f"{labels.get(scheme, scheme.upper())} {index + 1}"


def localized_profile_name(node: VPNNode, uri: str, index: int) -> str:
    raw_region = (node.region or node.name or "VPN").strip()
    parts = [part.strip() for part in raw_region.split("|", 1)]
    code = parts[0].upper() if len(parts[0]) == 2 and parts[0].isalpha() else ""
    countries = {"RU": "Россия", "SE": "Швеция"}
    country = parts[1] if len(parts) > 1 and parts[1] else countries.get(code, raw_region)
    flag = "".join(chr(127397 + ord(char)) for char in code) if code else "🌐"
    variant = profile_display_name(uri, index)
    variants = {
        "REALITY": "Reality",
        "HY2": "Hysteria2",
        "XHTTP": "xHTTP",
        "VLESS-WS": "VLESS WebSocket",
        "VMESS-WS": "VMess WebSocket",
        "TROJAN-GRPC": "Trojan gRPC",
    }
    variant = variants.get(variant.upper(), variant)
    if variant.upper().startswith("VLESS ") and "reality" in uri.lower():
        variant = "Reality"
    return f"{flag} {country} ({variant})"


def incy_profile_allowed(uri: str, region: str | None, name: str | None = None) -> bool:
    scheme = urlsplit(uri).scheme.lower()
    if scheme in _INCY_EXCLUDED_SCHEMES:
        return False
    region_code = (region or "").split("|", 1)[0].strip().upper()
    if region_code != "RU":
        return True
    label = name or profile_display_name(uri, 0)
    variant = label.rsplit("(", 1)[-1].removesuffix(")").strip()
    return variant in _INCY_RU_PROFILE_VARIANTS


def _safe_profile_scalar(value: object) -> str:
    text = str(value or "").strip()
    if not text or "\n" in text or "\r" in text:
        return ""
    return text


def _awg_proxy_config(proxy: dict) -> str | None:
    options = proxy.get("amnezia-wg-option")
    if proxy.get("type") != "wireguard" or not isinstance(options, dict):
        return None
    private_key = _safe_profile_scalar(proxy.get("private-key"))
    public_key = _safe_profile_scalar(proxy.get("public-key"))
    server = _safe_profile_scalar(proxy.get("server"))
    port = _safe_profile_scalar(proxy.get("port"))
    addresses = proxy.get("ip") or proxy.get("address") or []
    if isinstance(addresses, str):
        addresses = [addresses]
    address = ", ".join(
        item for item in (_safe_profile_scalar(value) for value in addresses) if item
    )
    if not all((private_key, public_key, server, port, address)):
        return None

    dns_values = proxy.get("dns") or ["1.1.1.1", "1.0.0.1"]
    if isinstance(dns_values, str):
        dns_values = [dns_values]
    dns = ", ".join(
        item for item in (_safe_profile_scalar(value) for value in dns_values) if item
    )
    lines = ["[Interface]", f"PrivateKey = {private_key}", f"Address = {address}"]
    if dns:
        lines.append(f"DNS = {dns}")
    mtu = _safe_profile_scalar(proxy.get("mtu"))
    if mtu:
        lines.append(f"MTU = {mtu}")
    for source, target in _AWG_OPTION_NAMES.items():
        value = _safe_profile_scalar(options.get(source))
        if value:
            lines.append(f"{target} = {value}")

    lines.extend(["", "[Peer]", f"PublicKey = {public_key}"])
    preshared_key = _safe_profile_scalar(proxy.get("pre-shared-key"))
    if preshared_key:
        lines.append(f"PresharedKey = {preshared_key}")
    lines.extend(
        [
            "AllowedIPs = 0.0.0.0/0, ::/0",
            f"Endpoint = {server}:{port}",
            "PersistentKeepalive = 25",
        ]
    )
    return "\n".join(lines) + "\n"


def kit_amnezia_profiles(body: bytes) -> list[dict]:
    try:
        document = yaml.safe_load(body)
    except yaml.YAMLError:
        return []
    if not isinstance(document, dict) or not isinstance(document.get("proxies"), list):
        return []
    profiles = []
    for proxy in document["proxies"]:
        if not isinstance(proxy, dict):
            continue
        config = _awg_proxy_config(proxy)
        if not config:
            continue
        name = _safe_profile_scalar(proxy.get("name")) or f"AmneziaWG {len(profiles) + 1}"
        encoded = urlsafe_b64encode(config.encode()).decode().rstrip("=")
        profiles.append(
            {
                "name": name[:80],
                "protocol": "vpn",
                "config": f"vpn://{encoded}#{quote(name[:80])}",
                "incy_config": f"amneziawg://{encoded}#{quote(name[:80])}",
                "wireguard_config": config,
            }
        )
    return profiles


async def kit_amnezia_profiles_for_client(
    client: VPNClient,
    node: VPNNode,
    node_config: VPNNodeConfig,
) -> list[dict]:
    try:
        target = await kit_subscription_target(client, node, node_config)
        if target is None:
            return []
        base, sub_id = target
        async with httpx.AsyncClient(timeout=15, follow_redirects=False) as session:
            response = await session.get(
                f"{base.rstrip('/')}/{quote(sub_id, safe='._@-')}",
                headers={"User-Agent": "clash-verge/v2", "Accept": "application/yaml"},
            )
        if response.status_code != 200 or len(response.content) > 2 * 1024 * 1024:
            return []
        return kit_amnezia_profiles(response.content)
    except (ThreeXUIError, httpx.HTTPError, KeyError, TypeError, ValueError):
        return []


async def kit_all_profiles_for_client(
    client: VPNClient,
    node: VPNNode,
    node_config: VPNNodeConfig,
    user_agent: str = "",
    accept: str = "",
) -> list[dict]:
    bundle = await kit_subscription_for_client(client, node, node_config, user_agent, accept)
    profiles = []
    for uri in bundle["uris"] if bundle else []:
        index = len(profiles)
        name = localized_profile_name(node, uri, index)
        profiles.append(
            {
                "name": name,
                "protocol": urlsplit(uri).scheme.lower(),
                "config": normalized_profile_uri(uri, index, name),
            }
        )
    for profile in await kit_amnezia_profiles_for_client(client, node, node_config):
        name = localized_profile_name(node, profile["config"], len(profiles))
        profile = {
            **profile,
            "name": name,
            "config": normalized_profile_uri(profile["config"], len(profiles), name),
            "incy_config": normalized_profile_uri(profile["incy_config"], len(profiles), name),
        }
        profiles.append(profile)
    unique = []
    seen = set()
    for profile in profiles:
        if profile["config"] not in seen:
            seen.add(profile["config"])
            unique.append(profile)
    return unique


async def alternate_client_profiles(
    db: AsyncSession,
    client: VPNClient,
    source_config: VPNNodeConfig,
) -> list[dict]:
    """Build extra public URIs for the same client identity on configured nodes."""
    raw_node_ids = source_config.config.get("alternate_node_ids", [])
    if not isinstance(raw_node_ids, list):
        return []
    try:
        node_ids = sorted({int(value) for value in raw_node_ids})
    except (TypeError, ValueError):
        return []
    node_ids = [value for value in node_ids if value > 0 and value != client.node_id]
    if source_config.config.get("alternate_profile_links") is True:
        node_ids.append(client.node_id)
    if not node_ids:
        return []

    result = await db.execute(
        select(VPNNode, VPNNodeConfig)
        .join(VPNNodeConfig, VPNNodeConfig.node_id == VPNNode.id)
        .where(
            VPNNode.id.in_(node_ids),
            VPNNode.status == "active",
            VPNNodeConfig.protocol == client.protocol,
        )
        .order_by(VPNNode.id)
    )
    profiles = []
    for node, node_config in result.all():
        link_host = str(node_config.config.get("share_address") or "").lower().strip()
        link_variants = {
            ("tuic", 443, ""): "TUIC",
            ("tuic", 8444, ""): "TUIC",
            ("wireguard", 51820, ""): "WireGuard",
        }
        selected: list[tuple[str, str]] = []
        if link_host and node_config.config.get("alternate_profile_links") is True:
            try:
                panel = ThreeXUIClient(node_config.config.get("api_address"), timeout=8)
                for link in await panel.get_client_links(f"vpn-{client.id}"):
                    parsed = urlsplit(link)
                    link_hostname = parsed.hostname
                    link_port = parsed.port
                    transport = parse_qs(parsed.query).get("type", [""])[0].lower()
                    if parsed.scheme.lower() == "vmess":
                        try:
                            raw = link.split("://", 1)[1]
                            details = json.loads(
                                b64decode(raw + "=" * (-len(raw) % 4)).decode()
                            )
                            link_hostname = str(details.get("add", "")).lower()
                            link_port = int(details.get("port", 0))
                            transport = str(details.get("net", "")).lower()
                        except (binascii.Error, UnicodeDecodeError, TypeError, ValueError):
                            continue
                    if (link_hostname or "").lower() != link_host:
                        continue
                    scheme = parsed.scheme.lower()
                    security = parse_qs(parsed.query).get("security", [""])[0].lower()
                    link_name = clean_profile_name(unquote(parsed.fragment)).upper()
                    variant = None
                    if scheme == "vless" and transport == "tcp" and security == "reality":
                        variant = "Reality 2" if "REALITY-2" in link_name else "Reality"
                    elif scheme == "vless" and transport in {"xhttp", "splithttp"}:
                        variant = "xHTTP"
                    elif scheme == "vless" and transport == "ws":
                        variant = "VLESS WebSocket"
                    elif scheme == "trojan" and transport == "grpc":
                        variant = "Trojan gRPC"
                    elif scheme == "vmess" and transport == "ws":
                        variant = "VMess WebSocket"
                    elif scheme in {"hysteria2", "hy2"}:
                        variant = "Hysteria2"
                    if variant is None:
                        variant = link_variants.get((scheme, link_port, transport))
                    if variant:
                        selected.append((link, variant))
            except (ThreeXUIError, TypeError, ValueError):
                selected = []
        if node.id == client.node_id:
            selected = [(uri, variant) for uri, variant in selected if variant != "Reality"]
        elif not selected:
            fallback = build_client_uri(client, node, node_config.config)
            if fallback:
                selected = [(fallback, "")]
        for uri, variant in selected:
            name = localized_profile_name(node, uri, len(profiles))
            if variant:
                name = name.split(" (", 1)[0] + f" ({variant})"
            profiles.append(
                {
                    "name": name,
                    "protocol": urlsplit(uri).scheme.lower(),
                    "config": normalized_profile_uri(uri, len(profiles), name),
                    "node_id": node.id,
                    "region": node.region,
                    "available": node_accepts_clients(node, management_mode="threexui"),
                    "latency_ms": node.latency_ms,
                }
            )
    return profiles


async def active_subscription_for_user(
    db: AsyncSession,
    user_id: int,
    now: datetime,
) -> Subscription | None:
    result = await db.execute(
        select(Subscription)
        .where(
            Subscription.user_id == user_id,
            Subscription.status == "active",
            Subscription.expires_at > now,
        )
        .order_by(Subscription.expires_at.desc(), Subscription.id.desc())
    )
    return result.scalars().first()


async def subscription_client_for_token(
    token: str,
    db: AsyncSession,
) -> tuple[VPNClient, Subscription]:
    """Resolve an old signed URL to the user's current active VPN client."""

    client_id = verify_incy_subscription_token(token, allow_expired=True)
    if client_id is None:
        raise HTTPException(status_code=404, detail="Subscription link is invalid")
    original = await db.get(VPNClient, client_id)
    if original is None:
        raise HTTPException(status_code=404, detail="Subscription client not found")

    now = datetime.now(timezone.utc)
    subscription = await active_subscription_for_user(db, original.user_id, now)
    if subscription is None:
        raise HTTPException(status_code=410, detail="Subscription is no longer active")
    client = None
    if (
        original.subscription_id == subscription.id
        and original.status == "active"
        and aware(original.expires_at) > now
    ):
        client = original
    if client is None:
        client = await db.scalar(
            select(VPNClient)
            .where(
                VPNClient.subscription_id == subscription.id,
                VPNClient.status == "active",
                VPNClient.expires_at > now,
            )
            .order_by(VPNClient.id.desc())
        )
    if client is None:
        raise HTTPException(status_code=410, detail="Subscription has no active VPN client")
    return client, subscription


async def require_device(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(device_bearer),
    db: AsyncSession = Depends(get_db),
) -> DevicePrincipal:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Device token required")
    digest = token_hash(credentials.credentials)
    result = await db.execute(
        select(ClientDevice).where(
            ClientDevice.token_hash == digest,
            ClientDevice.status == "active",
        )
    )
    device = result.scalar_one_or_none()
    now = datetime.now(timezone.utc)
    if device is None or not secrets.compare_digest(device.token_hash, digest):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid device token")
    if aware(device.expires_at) <= now:
        subscription = await active_subscription_for_user(db, device.user_id, now)
        if subscription is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid device token")
        device.expires_at = subscription.expires_at
    device.last_seen_at = now
    await db.commit()
    request.state.principal = type(
        "DeviceAuditPrincipal",
        (),
        {"kind": "device", "name": f"device-{device.id}"},
    )()
    return DevicePrincipal(device_id=device.id, user_id=device.user_id)


@router.post(
    "/activation-codes",
    response_model=ActivationCodeResponse,
    dependencies=[Depends(require_api_access)],
)
async def create_activation_code(data: ActivationCodeCreate, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.telegram_id == data.telegram_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    issued = await issue_provider_code(
        db,
        user,
        ttl_minutes=data.ttl_minutes,
        actor_type="service",
    )
    return ActivationCodeResponse(
        code=issued.code,
        device_name=issued.device_name,
        expires_at=issued.expires_at,
    )


@router.post("/activate", response_model=DeviceTokenResponse)
async def activate_device(data: DeviceActivate, db: AsyncSession = Depends(get_db)):
    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(ActivationCode)
        .where(ActivationCode.code_hash == token_hash(data.code))
        .with_for_update()
    )
    activation = result.scalar_one_or_none()
    if activation is None or activation.used_at is not None or aware(activation.expires_at) <= now:
        raise HTTPException(status_code=400, detail="Activation code is invalid or expired")
    subscription = await active_subscription_for_user(db, activation.user_id, now)
    if subscription is None:
        raise HTTPException(status_code=403, detail="No active subscription")
    token = generate_scoped_token("device", activation.user_id)
    expires_at = subscription.expires_at
    device = ClientDevice(
        user_id=activation.user_id,
        name=data.name,
        platform=data.platform,
        token_hash=token_hash(token),
        token_prefix=token.split(".", 1)[0],
        status="active",
        expires_at=expires_at,
        last_seen_at=now,
    )
    db.add(device)
    await db.flush()
    activation.used_at = now
    activation.device_id = device.id
    await db.commit()
    await write_audit(
        db,
        action="device.activate",
        result="success",
        actor_type="device",
        actor_id=f"device-{device.id}",
        resource_type="client_device",
        resource_id=device.id,
        details={"platform": device.platform},
    )
    return DeviceTokenResponse(device_id=device.id, access_token=token, expires_at=expires_at)


def build_client_uri(client: VPNClient, node: VPNNode, config: dict) -> str:
    if client.config_override:
        return client.config_override.strip()
    if client.protocol != "vless":
        return str(config.get("client_uri") or "").strip()
    host = config.get("host") or node.hostname or node.ip_address
    link_config = dict(config)
    link_config["fp"] = config.get("fp") or client.fingerprint or "firefox"
    if client.flow:
        link_config["flow"] = client.flow
    return build_vless_url(
        uuid=client.client_uuid,
        host=host,
        port=config.get("port", 443),
        config=link_config,
        remark=f"vpn-{client.id}",
    )


def provider_links() -> list[ClientProfileLink]:
    account_url = (
        settings.client_account_url.strip()
        or settings.provider_cabinet_url.strip()
        or f"{settings.public_base_url.rstrip('/')}/cabinet"
    )
    links = (
        ("channel", "Канал / Бот", settings.client_channel_url, "channel"),
        ("support", "Поддержка", settings.client_support_url or incy_support_url(), "support"),
        ("website", "Сайт", settings.client_website_url or f"{settings.public_base_url.rstrip('/')}/", "website"),
        ("account", "Продлить подписку", account_url, "account"),
    )
    return [
        ClientProfileLink(id=link_id, title=title, url=url.strip(), icon=icon)
        for link_id, title, url, icon in links
        if url and url.strip().startswith(("https://", "tg://"))
    ]


async def trial_plan(db: AsyncSession) -> Plan:
    code = f"mobile-trial-{settings.client_trial_days}d"
    result = await db.execute(select(Plan).where(Plan.code == code))
    plan = result.scalar_one_or_none()
    traffic_limit_gb = 0
    if settings.client_trial_traffic_limit_bytes:
        traffic_limit_gb = (
            settings.client_trial_traffic_limit_bytes + 1024**3 - 1
        ) // 1024**3
    if plan is not None:
        # Keep an existing trial plan aligned with deployment settings. This
        # matters when the quota is changed without changing the duration.
        if plan.duration_days != settings.client_trial_days:
            plan.duration_days = settings.client_trial_days
        if plan.traffic_limit_gb != traffic_limit_gb:
            plan.traffic_limit_gb = traffic_limit_gb
        return plan
    plan = Plan(
        code=code,
        name=f"Тестовый доступ на {settings.client_trial_days} дн.",
        duration_days=settings.client_trial_days,
        max_connections=1,
        traffic_limit_gb=traffic_limit_gb,
        price=Decimal("0"),
        currency="RUB",
        is_active=True,
        is_public=False,
    )
    db.add(plan)
    await db.flush()
    return plan


@router.post("/trial", response_model=DeviceTokenResponse)
async def activate_trial(data: TrialActivate, db: AsyncSession = Depends(get_db)):
    if not settings.client_trial_enabled:
        raise HTTPException(status_code=404, detail="Trial access is disabled")
    if data.platform not in {"android", "ios"}:
        raise HTTPException(status_code=400, detail="Unsupported platform")
    try:
        install_id = str(UUID(data.install_id))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid install identifier") from exc

    install_digest = token_hash(install_id)
    now = datetime.now(timezone.utc)
    existing = await db.scalar(
        select(ClientDevice)
        .where(ClientDevice.install_id_hash == install_digest)
        .with_for_update()
    )
    if existing is not None:
        subscription = await active_subscription_for_user(db, existing.user_id, now)
        if subscription is None:
            raise HTTPException(status_code=403, detail="Trial period has expired")
        plan = await db.get(Plan, subscription.plan_id)
        token = generate_scoped_token("device", existing.user_id)
        existing.token_hash = token_hash(token)
        existing.token_prefix = token.split(".", 1)[0]
        existing.status = "active"
        existing.expires_at = subscription.expires_at
        existing.last_seen_at = now
        await db.commit()
        return DeviceTokenResponse(
            device_id=existing.id,
            trial_id=(
                mobile_trial_id(existing.id)
                if plan is not None and plan.code.startswith("mobile-trial-")
                else None
            ),
            access_token=token,
            expires_at=existing.expires_at,
        )

    result = await db.execute(
        select(VPNNode, VPNNodeConfig)
        .join(VPNNodeConfig, VPNNodeConfig.node_id == VPNNode.id)
        .where(VPNNode.status == "active")
        .order_by(VPNNode.id, VPNNodeConfig.id)
    )
    eligible: list[tuple[VPNNode, VPNNodeConfig]] = []
    seen_profiles: set[tuple[int, str]] = set()
    for node, node_config in result.all():
        protocol = (node_config.protocol or "").strip().lower()
        profile_key = (node.id, protocol)
        if not protocol or profile_key in seen_profiles:
            continue
        if not node_accepts_clients(node, management_mode="threexui"):
            continue
        if protocol == "vless":
            if not node_config.config.get("api_address"):
                continue
        elif not str(node_config.config.get("client_uri") or "").strip():
            continue
        eligible.append((node, node_config))
        seen_profiles.add(profile_key)
    if not eligible:
        raise HTTPException(status_code=503, detail="No trial servers are available")

    synthetic_telegram_id = -(int(install_digest[:15], 16) + 1)
    user = await db.scalar(select(User).where(User.telegram_id == synthetic_telegram_id))
    if user is None:
        user = User(
            telegram_id=synthetic_telegram_id,
            username=f"mobile-trial-{install_digest[:12]}",
            first_name="Mobile trial",
        )
        db.add(user)
        await db.flush()

    provisioned: list[tuple[ThreeXUIClient, str, str]] = []
    try:
        plan = await trial_plan(db)
        expires_at = now + timedelta(days=settings.client_trial_days)
        subscription = Subscription(
            user_id=user.id,
            plan_id=plan.id,
            status="active",
            starts_at=now,
            expires_at=expires_at,
            traffic_limit_bytes=(
                settings.client_trial_traffic_limit_bytes
                if settings.client_trial_traffic_limit_bytes > 0
                else None
            ),
        )
        db.add(subscription)
        await db.flush()

        for node, node_config in eligible:
            protocol = node_config.protocol.strip().lower()
            client = VPNClient(
                user_id=user.id,
                subscription_id=subscription.id,
                node_id=node.id,
                protocol=protocol,
                client_type="universal",
                flow=str(node_config.config.get("flow") or ""),
                fingerprint=str(node_config.config.get("fp") or "chrome"),
                client_uuid=str(uuid4()),
                config_override=(
                    str(node_config.config.get("client_uri"))
                    if protocol != "vless"
                    else None
                ),
                traffic_limit_gb=plan.traffic_limit_gb,
                status="active",
                expires_at=expires_at,
            )
            db.add(client)
            await db.flush()
            if protocol == "vless":
                panel = ThreeXUIClient(address=node_config.config["api_address"])
                inbound_tag = str(node_config.config.get("inbound_tag", ""))
                email = f"vpn-{client.id}"
                await panel.add_vless_user(
                    inbound_tag=inbound_tag,
                    client_uuid=client.client_uuid,
                    email=email,
                    flow=client.flow,
                    expiry_time=int(expires_at.timestamp() * 1000),
                    telegram_id=user.telegram_id,
                    limit_ip=plan.max_connections,
                    total_gb=plan.traffic_limit_gb * 1024**3,
                )
                provisioned.append((panel, inbound_tag, email))

        token = generate_scoped_token("device", user.id)
        device = ClientDevice(
            user_id=user.id,
            name=data.name.strip(),
            platform=data.platform,
            token_hash=token_hash(token),
            token_prefix=token.split(".", 1)[0],
            install_id_hash=install_digest,
            status="active",
            expires_at=expires_at,
            last_seen_at=now,
        )
        db.add(device)
        await db.commit()
    except Exception as exc:
        await db.rollback()
        for panel, inbound_tag, email in reversed(provisioned):
            try:
                await panel.remove_vless_user(inbound_tag=inbound_tag, email=email)
            except ThreeXUIError:
                pass
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(status_code=502, detail="Unable to provision trial access") from exc

    await db.refresh(device)
    await write_audit(
        db,
        action="device.trial.activate",
        result="success",
        actor_type="device",
        actor_id=f"device-{device.id}",
        resource_type="subscription",
        resource_id=subscription.id,
        details={
            "platform": data.platform,
            "days": settings.client_trial_days,
            "profiles": len(eligible),
        },
    )
    return DeviceTokenResponse(
        device_id=device.id,
        trial_id=mobile_trial_id(device.id),
        access_token=token,
        expires_at=device.expires_at,
    )


@router.post("/import")
async def import_subscription_for_device(
    data: SubscriptionImport,
    principal: DevicePrincipal = Depends(require_device),
    db: AsyncSession = Depends(get_db),
):
    """Attach a signed provider subscription link to the current device.

    A mobile trial uses a synthetic user until the owner opens a signed
    subscription link. The link proves access to the target VPN client; the
    existing device bearer proves which installed app is being upgraded.
    Keeping the device row and token lets the client refresh its profile
    without exposing a service credential.
    """

    client, subscription = await subscription_client_for_token(data.token, db)
    now = datetime.now(timezone.utc)
    device = await db.get(ClientDevice, principal.device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Device not found")
    device.user_id = client.user_id
    device.status = "active"
    device.revoked_at = None
    device.expires_at = min(aware(subscription.expires_at), aware(client.expires_at))
    device.last_seen_at = now
    await db.commit()
    await write_audit(
        db,
        action="device.subscription.import",
        result="success",
        actor_type="device",
        actor_id=f"device-{device.id}",
        resource_type="subscription",
        resource_id=subscription.id,
        details={"client_id": client.id, "subscription_id": subscription.id},
    )
    return {
        "device_id": device.id,
        "subscription_id": subscription.id,
        "expires_at": device.expires_at,
    }


@router.get("/profile", response_model=ClientProfileResponse)
async def client_profile(
    request: Request,
    principal: DevicePrincipal = Depends(require_device),
    db: AsyncSession = Depends(get_db),
):
    now = datetime.now(timezone.utc)
    subscription = await active_subscription_for_user(db, principal.user_id, now)
    if subscription is None:
        raise HTTPException(status_code=403, detail="No active subscription")
    result = await db.execute(
        select(VPNClient, VPNNode, VPNNodeConfig)
        .join(VPNNode, VPNNode.id == VPNClient.node_id)
        .join(
            VPNNodeConfig,
            (VPNNodeConfig.node_id == VPNClient.node_id)
            & (VPNNodeConfig.protocol == VPNClient.protocol),
        )
        .where(
            VPNClient.subscription_id == subscription.id,
            VPNClient.status == "active",
            VPNClient.expires_at > now,
        )
    )
    plan = await db.get(Plan, subscription.plan_id)
    total_bytes = subscription.traffic_limit_bytes
    if total_bytes is None and plan is not None and plan.traffic_limit_gb:
        total_bytes = plan.traffic_limit_gb * 1024**3
    nodes = []
    sensitive_configs = []
    observed_panel_totals: list[int] = []
    configured_client_totals: list[int] = []
    upload_bytes = 0
    download_bytes = 0
    active_vpn_ids: list[int] = []
    for client, node, node_config in result.all():
        uri = build_client_uri(client, node, node_config.config)
        if not uri:
            continue
        active_vpn_ids.append(client.id)
        configured_total = max(int(client.traffic_limit_gb or 0), 0) * 1024**3
        if configured_total:
            configured_client_totals.append(configured_total)
        client_upload = max(int(client.upload_bytes or 0), 0)
        client_download = max(int(client.download_bytes or 0), 0)
        if node_config.config.get("api_address") and client.protocol == "vless":
            try:
                traffic = await ThreeXUIClient(
                    node_config.config["api_address"]
                ).get_client_traffic(f"vpn-{client.id}")
                client_upload = max(int(traffic.get("up", 0) or 0), 0)
                client_download = max(int(traffic.get("down", 0) or 0), 0)
                panel_total = max(int(traffic.get("total", 0) or 0), 0)
                if panel_total:
                    observed_panel_totals.append(panel_total)
                client.upload_bytes = client_upload
                client.download_bytes = client_download
            except (ThreeXUIError, TypeError, ValueError):
                pass
        upload_bytes += client_upload
        download_bytes += client_download
        kit_subscription = await kit_subscription_for_client(
            client,
            node,
            node_config,
            request.headers.get("user-agent", ""),
            request.headers.get("accept", ""),
        )
        profile_uris = (
            [
                profile_uri
                for profile_uri in kit_subscription["uris"]
                if urlsplit(profile_uri).scheme.lower() in _FREEDOM_PROFILE_SCHEMES
            ]
            if kit_subscription
            else []
        ) or [uri]
        available = node_accepts_clients(node, management_mode="threexui")
        for index, profile_uri in enumerate(profile_uris):
            scheme = urlsplit(profile_uri).scheme.lower()
            multi_profile = kit_subscription is not None
            name = localized_profile_name(node, profile_uri, index)
            profile_uri = normalized_profile_uri(profile_uri, index, name)
            nodes.append(
                ClientProfileNode(
                    profile_id=(
                        f"{node.id}:{scheme}:{index}"
                        if multi_profile
                        else f"{node.id}:{client.protocol}"
                    ),
                    node_id=node.id,
                    name=name,
                    region=node.region,
                    available=available,
                    latency_ms=node.latency_ms,
                    protocol=scheme if multi_profile else client.protocol,
                    config=profile_uri,
                    upload_bytes=client_upload if index == 0 else 0,
                    download_bytes=client_download if index == 0 else 0,
                )
            )
            sensitive_configs.append({"node_id": node.id, "vpn_uri": profile_uri})
        alternate_profiles = await alternate_client_profiles(db, client, node_config)
        for profile in alternate_profiles:
            if profile["protocol"] not in _FREEDOM_PROFILE_SCHEMES:
                continue
            parsed = urlsplit(profile["config"])
            transport = parse_qs(parsed.query).get("type", ["tcp"])[0]
            profile_id = f"{client.id}:{profile['node_id']}:alternate"
            if (profile["protocol"], parsed.port, transport) != ("vless", 8443, "tcp"):
                profile_id += f":{profile['protocol']}:{parsed.port}:{transport}"
            nodes.append(
                ClientProfileNode(
                    profile_id=profile_id,
                    node_id=profile["node_id"],
                    name=profile["name"],
                    region=profile["region"],
                    available=profile["available"],
                    latency_ms=profile["latency_ms"],
                    protocol=profile["protocol"],
                    config=profile["config"],
                    upload_bytes=0,
                    download_bytes=0,
                )
            )
            sensitive_configs.append(
                {"node_id": profile["node_id"], "vpn_uri": profile["config"]}
            )
    # 3x-ui is authoritative for a paid client's accumulated quota. A
    # renewal can increase the client's totalGB while the original plan and
    # subscription rows still contain the old package limit.
    if observed_panel_totals:
        total_bytes = max(observed_panel_totals)
    elif total_bytes is None and configured_client_totals:
        total_bytes = max(configured_client_totals)
    await db.commit()
    await write_audit(
        db,
        action="device.profile.read",
        result="success",
        actor_type="device",
        actor_id=f"device-{principal.device_id}",
        resource_type="subscription",
        resource_id=subscription.id,
        details={"nodes": len(nodes)},
        sensitive_details={"configs": sensitive_configs},
    )
    return ClientProfileResponse(
        device_id=principal.device_id,
        trial_id=(
            mobile_trial_id(principal.device_id)
            if plan is not None and plan.code.startswith("mobile-trial-")
            else None
        ),
        user_id=principal.user_id,
        subscription_id=subscription.id,
        vpn_id=str(max(active_vpn_ids)) if active_vpn_ids else None,
        expires_at=subscription.expires_at,
        provider=ClientProviderInfo(
            name=settings.client_provider_name.strip() or settings.provider_name,
            support_url=incy_support_url(),
            cabinet_url=(
                settings.provider_cabinet_url.strip()
                or f"{settings.public_base_url.rstrip('/')}/cabinet"
            ),
        ),
        provider_name=settings.client_provider_name.strip() or settings.provider_name,
        plan_name=plan.name if plan is not None else "",
        announcement=settings.client_announcement,
        announcement_url=settings.client_announcement_url.strip() or None,
        usage=ClientProfileUsage(
            upload_bytes=upload_bytes,
            download_bytes=download_bytes,
            total_bytes=total_bytes,
            remaining_bytes=(
                max(total_bytes - upload_bytes - download_bytes, 0)
                if total_bytes is not None
                else None
            ),
        ),
        links=provider_links(),
        updated_at=now,
        nodes=nodes,
    )


@router.get("/subscription/{token}")
async def incy_subscription(
    token: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Return an INCY-compatible subscription for a signed client link."""

    client, _subscription = await subscription_client_for_token(token, db)

    node = await db.get(VPNNode, client.node_id)
    if node is None:
        raise HTTPException(status_code=404, detail="VPN node not found")
    node_config = await db.scalar(
        select(VPNNodeConfig).where(
            VPNNodeConfig.node_id == client.node_id,
            VPNNodeConfig.protocol == client.protocol,
        )
    )
    if node_config is None:
        raise HTTPException(status_code=404, detail="VPN node configuration not found")

    uri = build_client_uri(client, node, node_config.config)
    kit_subscription = await kit_subscription_for_client(
        client,
        node,
        node_config,
        request.headers.get("user-agent", ""),
        request.headers.get("accept", ""),
    )
    alternate_profiles = await alternate_client_profiles(db, client, node_config)
    profile_title = settings.provider_name.strip() or "Freedom VPN"
    site_url = f"{settings.public_base_url.rstrip('/')}/"
    cabinet_url = (
        settings.provider_cabinet_url.strip()
        or f"{settings.public_base_url.rstrip('/')}/cabinet"
    )
    support_url = incy_support_url()
    expires_at = int(aware(client.expires_at).timestamp())
    traffic_upload_bytes = 0
    traffic_download_bytes = 0
    traffic_total_bytes = max(int(client.traffic_limit_gb or 0), 0) * 1024**3
    if traffic_total_bytes:
        try:
            traffic = await ThreeXUIClient(
                node_config.config.get("api_address")
            ).get_client_traffic(f"vpn-{client.id}")
            traffic_upload_bytes = max(int(traffic.get("up", 0) or 0), 0)
            traffic_download_bytes = max(int(traffic.get("down", 0) or 0), 0)
        except (ThreeXUIError, TypeError, ValueError):
            # Keep the quota visible when 3x-ui is temporarily unavailable;
            # never report a fabricated amount of consumed traffic.
            pass
    if kit_subscription:
        incy_uris = [
            profile_uri
            for profile_uri in kit_subscription["uris"]
            if incy_profile_allowed(
                profile_uri,
                node.region,
                localized_profile_name(node, profile_uri, 0),
            )
        ]
        if not incy_uris:
            incy_uris = [uri]
        app_uris = [
            normalized_profile_uri(
                profile_uri,
                index,
                localized_profile_name(node, profile_uri, index),
            )
            for index, profile_uri in enumerate(incy_uris)
        ]
        offset = len(app_uris)
        app_uris.extend(
            normalized_profile_uri(profile["config"], offset + index)
            for index, profile in enumerate(alternate_profiles)
            if incy_profile_allowed(
                profile["config"], profile.get("region"), profile.get("name")
            )
        )
        body = "\n".join(app_uris) + "\n"
    else:
        app_uris = [normalized_profile_uri(uri, 0, localized_profile_name(node, uri, 0))]
        offset = len(app_uris)
        app_uris.extend(
            normalized_profile_uri(profile["config"], offset + index)
            for index, profile in enumerate(alternate_profiles)
            if incy_profile_allowed(
                profile["config"], profile.get("region"), profile.get("name")
            )
        )
        body = "\n".join(
            [
                f"#profile-title: {profile_title}",
                f"#support-url: {support_url}",
                f"#profile-web-page-url: {site_url}",
                f"#profile-update-interval: 6",
                *app_uris,
                "",
            ]
        )
    subscription_userinfo = (
        kit_subscription["headers"].get("subscription-userinfo")
        if kit_subscription
        else None
    ) or (
        f"upload={traffic_upload_bytes}; "
        f"download={traffic_download_bytes}; "
        f"total={traffic_total_bytes}; expire={expires_at}"
    )
    return Response(
        content=body,
        media_type=(
            kit_subscription["content_type"]
            if kit_subscription
            else "text/plain"
        ),
        headers={
            "profile-title": incy_metadata_header(profile_title),
            "profile-update-interval": "6",
            "support-url": support_url,
            "profile-web-page-url": site_url,
            "premium-url": cabinet_url,
            "subscription-userinfo": subscription_userinfo,
            "content-disposition": 'inline; filename="freedom-vpn-incy.txt"',
            "cache-control": "no-store",
        },
    )


@router.get("/import/{token}", include_in_schema=False)
async def incy_import_redirect(
    token: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Bridge Telegram's HTTPS-only button validation to the INCY deep link.

    Telegram's Android WebView renders a 302 to ``incy://`` as
    ``ERR_UNKNOWN_URL_SCHEME`` instead of handing it to Android. Return a
    normal HTTPS page with an Android ``intent://`` action so the WebView can
    launch the installed app from an explicit user tap.
    """

    await subscription_client_for_token(token, db)

    subscription_url = (
        f"{settings.public_base_url.rstrip('/')}/v1/client/subscription/"
        f"{token}"
    )
    encoded_fallback_url = quote(subscription_url, safe="")
    encoded_subscription_url = quote(subscription_url, safe=":/?@&=,+-._~%")
    direct_link = build_incy_import_link(subscription_url)
    android_intent = (
        f"intent://import/{encoded_subscription_url}"
        "#Intent;scheme=incy;package=llc.itdev.incy;"
        "action=android.intent.action.VIEW;category=android.intent.category.BROWSABLE;"
        f"S.browser_fallback_url={encoded_fallback_url};end"
    )
    user_agent = request.headers.get("user-agent", "").lower()
    launch_link = android_intent if "android" in user_agent else direct_link
    return HTMLResponse(
        content=f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Импорт в INCY</title>
  <style>
    :root {{ color-scheme: dark; }}
    body {{ margin: 0; min-height: 100vh; display: grid; place-items: center; background: #162331; color: #f4f7fb; font: 16px system-ui, sans-serif; }}
    main {{ width: min(420px, calc(100% - 40px)); text-align: center; }}
    h1 {{ margin: 0 0 12px; font-size: 28px; }}
    p {{ color: #b9c5d2; line-height: 1.5; }}
    a {{ display: block; margin-top: 20px; padding: 15px 20px; border-radius: 12px; background: #2596e8; color: white; font-weight: 700; text-decoration: none; }}
    .fallback {{ margin-top: 14px; padding: 0; background: transparent; color: #8fcaff; font-weight: 500; }}
  </style>
</head>
<body>
  <main>
    <h1>Импорт конфигурации</h1>
    <p>Нажмите кнопку, чтобы открыть конфигурацию в установленном приложении INCY.</p>
    <a href="{html.escape(launch_link, quote=True)}">Открыть в INCY</a>
  </main>
</body>
</html>""",
        status_code=status.HTTP_200_OK,
        headers={"cache-control": "no-store"},
    )


async def _subscription_client_context(
    token: str,
    db: AsyncSession,
) -> tuple[VPNClient, VPNNode, VPNNodeConfig]:
    client, _subscription = await subscription_client_for_token(token, db)
    node = await db.get(VPNNode, client.node_id)
    node_config = await db.scalar(
        select(VPNNodeConfig).where(
            VPNNodeConfig.node_id == client.node_id,
            VPNNodeConfig.protocol == client.protocol,
        )
    )
    if node is None or node_config is None:
        raise HTTPException(status_code=404, detail="VPN node configuration not found")
    return client, node, node_config


@router.get("/v2raytun-import/{token}", include_in_schema=False)
async def v2raytun_import_redirect(
    token: str,
    db: AsyncSession = Depends(get_db),
):
    await _subscription_client_context(token, db)
    subscription_url = f"{settings.public_base_url.rstrip('/')}/v1/client/subscription/{token}"
    deep_link = f"v2raytun://import/{quote(subscription_url, safe=':/?@&=,+-._~%')}"
    return HTMLResponse(
        content=f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Импорт в V2rayTUN</title><style>:root{{color-scheme:dark}}body{{margin:0;min-height:100vh;display:grid;place-items:center;background:#101820;color:#f5f7fb;font:16px system-ui,sans-serif}}main{{width:min(420px,calc(100% - 40px));text-align:center}}p{{color:#b8c4d0;line-height:1.5}}a{{display:block;margin-top:20px;padding:15px 20px;border-radius:8px;background:#2878ff;color:#fff;font-weight:700;text-decoration:none}}</style></head>
<body><main><h1>Импорт конфигурации</h1><p>Нажмите кнопку, чтобы добавить подписку в V2rayTUN.</p><a href="{html.escape(deep_link, quote=True)}">Открыть в V2rayTUN</a></main></body></html>""",
        headers={"cache-control": "no-store"},
    )


@router.get("/amnezia-import/{token}", include_in_schema=False)
async def amnezia_import_redirect(
    token: str,
    db: AsyncSession = Depends(get_db),
):
    client, node, node_config = await _subscription_client_context(token, db)
    profiles = await kit_amnezia_profiles_for_client(client, node, node_config)
    if not profiles:
        raise HTTPException(status_code=404, detail="AmneziaWG profiles are unavailable")
    buttons = "".join(
        f'<a href="{html.escape(profile["config"], quote=True)}">Открыть {html.escape(profile["name"])}</a>'
        for profile in profiles
    )
    router_links = "".join(
        f'<a class="secondary" href="/v1/client/router-awg/{html.escape(token, quote=True)}/{index}">Скачать {html.escape(profile["name"])} для роутера</a>'
        for index, profile in enumerate(profiles)
    )
    return HTMLResponse(
        content=f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Импорт в Amnezia VPN</title><style>:root{{color-scheme:dark}}body{{margin:0;min-height:100vh;display:grid;place-items:center;background:#12171d;color:#f7f8fa;font:16px system-ui,sans-serif}}main{{width:min(460px,calc(100% - 40px));text-align:center;padding:28px 0}}p{{color:#bac3ce;line-height:1.5}}a{{display:block;margin-top:12px;padding:15px 18px;border-radius:8px;background:#ffd43b;color:#141414;font-weight:750;text-decoration:none}}a.secondary{{background:transparent;color:#b9d4ff;border:1px solid #526173;font-weight:600}}</style></head>
<body><main><h1>Выберите AmneziaWG</h1><p>Обычный профиль совместим с большинством устройств. Версию 3.1 выбирайте, если ее поддерживает приложение.</p>{buttons}<p>Для Podkop или другого роутера:</p>{router_links}</main></body></html>""",
        headers={"cache-control": "no-store"},
    )


@router.get("/router-awg/{token}/{profile_index}", include_in_schema=False)
async def router_awg_config(
    token: str,
    profile_index: int,
    db: AsyncSession = Depends(get_db),
):
    client, node, node_config = await _subscription_client_context(token, db)
    profiles = await kit_amnezia_profiles_for_client(client, node, node_config)
    if profile_index < 0 or profile_index >= len(profiles):
        raise HTTPException(status_code=404, detail="AmneziaWG profile not found")
    profile = profiles[profile_index]
    filename = "amneziawg-3.conf" if "3.1" in profile["name"] else "amneziawg.conf"
    return Response(
        content=profile["wireguard_config"],
        media_type="text/plain",
        headers={
            "content-disposition": f'attachment; filename="{filename}"',
            "cache-control": "no-store",
        },
    )


@router.get("/qr/{token}/{profile_index}", include_in_schema=False)
async def client_profile_qr(
    token: str,
    profile_index: int,
    db: AsyncSession = Depends(get_db),
):
    client, node, node_config = await _subscription_client_context(token, db)
    profiles = await kit_all_profiles_for_client(client, node, node_config, "Freedom VPN cabinet")
    if not profiles:
        fallback = build_client_uri(client, node, node_config.config)
        profiles = [
            {"config": normalized_profile_uri(
                fallback, 0, localized_profile_name(node, fallback, 0)
            )}
        ] if fallback else []
    profiles.extend(await alternate_client_profiles(db, client, node_config))
    if profile_index < 0 or profile_index >= len(profiles):
        raise HTTPException(status_code=404, detail="VPN profile not found")
    image = qrcode.make(profiles[profile_index]["config"])
    output = BytesIO()
    image.save(output, format="PNG")
    return Response(
        content=output.getvalue(),
        media_type="image/png",
        headers={"cache-control": "no-store"},
    )


@router.get("/freevpn-import/{token}", include_in_schema=False)
async def freevpn_import_redirect(
    token: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Bridge an HTTPS Telegram/site click to the Freedom VPN deep link."""

    await subscription_client_for_token(token, db)

    subscription_url = (
        f"{settings.public_base_url.rstrip('/')}/v1/client/subscription/"
        f"{token}"
    )
    encoded_fallback_url = quote(subscription_url, safe="")
    encoded_subscription_url = quote(subscription_url, safe=":/?@&=,+-._~%")
    direct_link = build_freevpn_import_link(subscription_url)
    android_intent = (
        f"intent://import/{encoded_subscription_url}"
        "#Intent;scheme=freevpn;package=org.freedomvpn.app;"
        "action=android.intent.action.VIEW;category=android.intent.category.BROWSABLE;"
        f"S.browser_fallback_url={encoded_fallback_url};end"
    )
    user_agent = request.headers.get("user-agent", "").lower()
    launch_link = android_intent if "android" in user_agent else direct_link
    return HTMLResponse(
        content=f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Импорт в Freedom VPN</title>
  <style>
    :root {{ color-scheme: dark; }}
    body {{ margin: 0; min-height: 100vh; display: grid; place-items: center; background: #061936; color: #f4f7fb; font: 16px system-ui, sans-serif; }}
    main {{ width: min(420px, calc(100% - 40px)); text-align: center; }}
    h1 {{ margin: 0 0 12px; font-size: 28px; }}
    p {{ color: #b9c5d2; line-height: 1.5; }}
    a {{ display: block; margin-top: 20px; padding: 15px 20px; border-radius: 12px; background: #28dfb0; color: #05203a; font-weight: 700; text-decoration: none; }}
  </style>
</head>
<body>
  <main>
    <h1>Импорт конфигурации</h1>
    <p>Нажмите кнопку, чтобы открыть подписку в установленном приложении Freedom VPN.</p>
    <a id="open-freedom-vpn" href="{html.escape(launch_link, quote=True)}">Открыть в Freedom VPN</a>
  </main>
</body>
</html>""",
        status_code=status.HTTP_200_OK,
        headers={"cache-control": "no-store"},
    )


@router.post("/refresh", response_model=DeviceTokenResponse)
async def refresh_device_token(
    principal: DevicePrincipal = Depends(require_device),
    db: AsyncSession = Depends(get_db),
):
    device = await db.get(ClientDevice, principal.device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Device not found")
    now = datetime.now(timezone.utc)
    subscription = await active_subscription_for_user(db, device.user_id, now)
    if subscription is None:
        raise HTTPException(status_code=403, detail="No active subscription")
    plan = await db.get(Plan, subscription.plan_id)
    token = generate_scoped_token("device", device.user_id)
    device.token_hash = token_hash(token)
    device.token_prefix = token.split(".", 1)[0]
    device.expires_at = subscription.expires_at
    await db.commit()
    return DeviceTokenResponse(
        device_id=device.id,
        access_token=token,
        expires_at=device.expires_at,
        trial_id=(
            mobile_trial_id(device.id)
            if plan is not None and plan.code.startswith("mobile-trial-")
            else None
        ),
    )
