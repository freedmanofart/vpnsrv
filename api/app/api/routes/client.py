import secrets
import html
from base64 import b64encode
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from urllib.parse import quote
from uuid import UUID, uuid4

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
    TrialActivate,
)
from app.services.audit import write_audit
from app.services.node_health import node_accepts_clients
from app.services.provider_codes import issue_provider_code
from app.services.threexui import ThreeXUIClient, ThreeXUIError
from app.services.vless import build_vless_url
from app.services.incy import build_incy_import_link
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
        ("account", "Личный кабинет", account_url, "account"),
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
        token = generate_scoped_token("device", existing.user_id)
        existing.token_hash = token_hash(token)
        existing.token_prefix = token.split(".", 1)[0]
        existing.status = "active"
        existing.expires_at = subscription.expires_at
        existing.last_seen_at = now
        await db.commit()
        return DeviceTokenResponse(
            device_id=existing.id,
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
        access_token=token,
        expires_at=device.expires_at,
    )


@router.get("/profile", response_model=ClientProfileResponse)
async def client_profile(
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
    upload_bytes = 0
    download_bytes = 0
    for client, node, node_config in result.all():
        uri = build_client_uri(client, node, node_config.config)
        if not uri:
            continue
        client_upload = max(int(client.upload_bytes or 0), 0)
        client_download = max(int(client.download_bytes or 0), 0)
        if node_config.config.get("api_address") and client.protocol == "vless":
            try:
                traffic = await ThreeXUIClient(
                    node_config.config["api_address"]
                ).get_client_traffic(f"vpn-{client.id}")
                client_upload = max(int(traffic.get("up", 0) or 0), 0)
                client_download = max(int(traffic.get("down", 0) or 0), 0)
                client.upload_bytes = client_upload
                client.download_bytes = client_download
            except (ThreeXUIError, TypeError, ValueError):
                pass
        upload_bytes += client_upload
        download_bytes += client_download
        nodes.append(
            ClientProfileNode(
                profile_id=f"{node.id}:{client.protocol}",
                node_id=node.id,
                name=node.name,
                region=node.region,
                available=node_accepts_clients(
                    node, management_mode="threexui"
                ),
                latency_ms=node.latency_ms,
                protocol=client.protocol,
                config=uri,
                upload_bytes=client_upload,
                download_bytes=client_download,
            )
        )
        sensitive_configs.append({"node_id": node.id, "vpn_uri": uri})
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
        user_id=principal.user_id,
        subscription_id=subscription.id,
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
async def incy_subscription(token: str, db: AsyncSession = Depends(get_db)):
    """Return an INCY-compatible subscription for a signed client link."""

    client_id = verify_incy_subscription_token(token)
    if client_id is None:
        raise HTTPException(status_code=404, detail="Subscription link is invalid or expired")

    client = await db.get(VPNClient, client_id)
    now = datetime.now(timezone.utc)
    if (
        client is None
        or client.status != "active"
        or aware(client.expires_at) <= now
    ):
        raise HTTPException(status_code=404, detail="Subscription is inactive or expired")

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
    region = (node.region or "Швеция").split("|", 1)[-1]
    warning = "Из-за блокировок РКН наш сервис может работать нестабильно."
    profile_title = f"{settings.provider_name} · {region}"
    site_url = f"{settings.public_base_url.rstrip('/')}/"
    cabinet_url = (
        settings.provider_cabinet_url.strip()
        or f"{settings.public_base_url.rstrip('/')}/cabinet"
    )
    bot_url = (
        f"https://t.me/{settings.bot_username.lstrip('@').strip()}"
        if settings.bot_username.strip()
        else ""
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
    body = "\n".join(
        [
            f"#profile-title: {profile_title}",
            f"#profile-description: {warning}",
            f"#support-url: {support_url}",
            f"#profile-web-page-url: {site_url}",
            f"#announce: {warning}",
            f"#announce-url: {bot_url}",
            f"#profile-update-interval: 6",
            uri,
            "",
        ]
    )
    return Response(
        content=body,
        media_type="text/plain",
        headers={
            "profile-title": incy_metadata_header(profile_title),
            "profile-description": incy_metadata_header(warning),
            "profile-update-interval": "6",
            "support-url": support_url,
            "profile-web-page-url": site_url,
            "announce": incy_metadata_header(warning),
            "announce-url": bot_url,
            "premium-url": cabinet_url,
            "subscription-userinfo": (
                f"upload={traffic_upload_bytes}; "
                f"download={traffic_download_bytes}; "
                f"total={traffic_total_bytes}; expire={expires_at}"
            ),
            "content-disposition": 'inline; filename="freedom-vpn-incy.txt"',
            "cache-control": "no-store",
        },
    )


@router.get("/import/{token}", include_in_schema=False)
async def incy_import_redirect(token: str):
    """Bridge Telegram's HTTPS-only button validation to the INCY deep link.

    Telegram's Android WebView renders a 302 to ``incy://`` as
    ``ERR_UNKNOWN_URL_SCHEME`` instead of handing it to Android. Return a
    normal HTTPS page with an Android ``intent://`` action so the WebView can
    launch the installed app from an explicit user tap.
    """

    if verify_incy_subscription_token(token) is None:
        raise HTTPException(status_code=404, detail="Subscription link is invalid or expired")

    subscription_url = (
        f"{settings.public_base_url.rstrip('/')}/v1/client/subscription/"
        f"{token}"
    )
    deep_link = build_incy_import_link(subscription_url)
    encoded_subscription_url = quote(subscription_url, safe=":/?@&=,+-._~%")
    android_intent = (
        f"intent://import/{encoded_subscription_url}"
        "#Intent;scheme=incy;package=llc.itdev.incy;end"
    )
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
    <a href="{html.escape(android_intent, quote=True)}">Открыть в INCY</a>
    <a class="fallback" href="{html.escape(deep_link, quote=True)}">Открыть обычной ссылкой</a>
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
    token = generate_scoped_token("device", device.user_id)
    device.token_hash = token_hash(token)
    device.token_prefix = token.split(".", 1)[0]
    device.expires_at = subscription.expires_at
    await db.commit()
    return DeviceTokenResponse(
        device_id=device.id,
        access_token=token,
        expires_at=device.expires_at,
    )
