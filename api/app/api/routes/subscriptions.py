from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.user import User
from app.db.models.plan import Plan
from app.db.models.subscription import Subscription
from app.db.models.vpn_client import VPNClient
from app.db.models.vpn_node import VPNNode
from app.db.models.vpn_node_config import VPNNodeConfig
from app.db.models.audit import AccessGrant

from app.db.session import get_db

from app.services.threexui import (
    ThreeXUIClient,
    ThreeXUIError,
    ThreeXUIClientNotFound,
)
from app.services.provisioning import (
    ProvisioningConflict,
    ProvisioningInvalid,
    ProvisioningNotFound,
    ProvisioningThreeXUIError,
    commit_provisioning,
    provision_subscription,
)

from app.schemas.subscription import (
    SubscriptionCreate,
    SubscriptionResponse,
    AccessGrantCreate,
    VPNClientRotate,
)
from app.schemas.vpn import VPNClientResponse
from app.core.security import require_api_access
from app.core.config import settings
from app.services.audit import write_audit
from app.services.node_health import node_accepts_clients


router = APIRouter(
    prefix="/subscriptions",
    tags=["Subscriptions"],
    dependencies=[Depends(require_api_access)],
)


def promo_catalog() -> dict[str, int]:
    result: dict[str, int] = {}
    for item in settings.promo_codes.split(","):
        code, separator, raw_days = item.strip().partition(":")
        if not separator:
            continue
        try:
            days = int(raw_days)
        except ValueError:
            continue
        if code and 1 <= days <= 365:
            result[code.upper()] = days
    return result


async def system_access_plan(db: AsyncSession, days: int) -> Plan:
    code = f"system-access-{days}d"
    result = await db.execute(select(Plan).where(Plan.code == code))
    plan = result.scalar_one_or_none()
    if plan is not None:
        return plan
    try:
        async with db.begin_nested():
            plan = Plan(
                code=code,
                name=f"Служебный доступ на {days} дн.",
                duration_days=days,
                price=Decimal("0"),
                currency="RUB",
                is_active=True,
                is_public=False,
            )
            db.add(plan)
            await db.flush()
            return plan
    except IntegrityError:
        result = await db.execute(select(Plan).where(Plan.code == code))
        return result.scalar_one()


def aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


@router.post("/access-grants", response_model=SubscriptionResponse)
async def grant_trial_or_promo(
    data: AccessGrantCreate,
    db: AsyncSession = Depends(get_db),
):
    user_result = await db.execute(
        select(User).where(User.telegram_id == data.telegram_id).with_for_update()
    )
    user = user_result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    kind = data.kind.lower()
    if kind == "trial":
        code = "TRIAL"
        days = 3
        previous = await db.execute(
            select(Subscription.id).where(Subscription.user_id == user.id).limit(1)
        )
        if previous.scalar_one_or_none() is not None:
            raise HTTPException(status_code=409, detail="Trial is available only once before purchase")
    elif kind == "promo":
        code = (data.code or "").strip().upper()
        days = promo_catalog().get(code, 0)
        if not days:
            raise HTTPException(status_code=404, detail="Promo code is invalid")
    else:
        raise HTTPException(status_code=400, detail="Unsupported access grant")

    used_result = await db.execute(
        select(AccessGrant.id).where(
            AccessGrant.user_id == user.id,
            AccessGrant.kind == kind,
            AccessGrant.code == code,
        )
    )
    if used_result.scalar_one_or_none() is not None:
        raise HTTPException(status_code=409, detail="Access grant was already used")

    now = datetime.now(timezone.utc)
    active_result = await db.execute(
        select(Subscription).where(
            Subscription.user_id == user.id,
            Subscription.status == "active",
            Subscription.expires_at > now,
        )
    )
    active = active_result.scalar_one_or_none()
    grant = AccessGrant(
        user_id=user.id,
        kind=kind,
        code=code,
        duration_days=days,
    )
    db.add(grant)

    if active is not None:
        if kind == "trial":
            raise HTTPException(status_code=409, detail="Active subscription already exists")
        active.expires_at = max(aware(active.expires_at), now) + timedelta(days=days)
        client_result = await db.execute(
            select(VPNClient).where(
                VPNClient.subscription_id == active.id,
                VPNClient.status == "active",
            )
        )
        for client in client_result.scalars():
            client.expires_at = active.expires_at
        grant.subscription_id = active.id
        await db.commit()
        subscription = active
    else:
        plan = await system_access_plan(db, days)
        try:
            result = await provision_subscription(
                db,
                user_id=user.id,
                plan_id=plan.id,
                node_id=data.node_id,
                client_type=data.client_type,
                flow=data.flow,
                fingerprint=data.fingerprint,
            )
        except ProvisioningNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ProvisioningConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ProvisioningInvalid as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except ProvisioningThreeXUIError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        grant.subscription_id = result.subscription.id
        await commit_provisioning(db, result)
        subscription = result.subscription

    await db.refresh(subscription)
    await write_audit(
        db,
        action=f"access_grant.{kind}",
        result="success",
        actor_type="service",
        resource_type="subscription",
        resource_id=subscription.id,
        details={"days": days, "code": code},
    )
    return subscription


# =========================================================
# Helpers
# =========================================================


async def get_user(
    db: AsyncSession,
    user_id: int,
) -> User:
    result = await db.execute(
        select(User).where(
            User.id == user_id
        )
    )

    user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(
            status_code=404,
            detail="User not found",
        )

    return user


async def get_plan(
    db: AsyncSession,
    plan_id: int,
) -> Plan:
    result = await db.execute(
        select(Plan).where(
            Plan.id == plan_id,
            Plan.is_active.is_(True),
        )
    )

    plan = result.scalar_one_or_none()

    if not plan:
        raise HTTPException(
            status_code=404,
            detail="Plan not found",
        )

    return plan


async def get_node_for_client(
    db: AsyncSession,
    node_id: int,
    protocol: str,
):
    result = await db.execute(
        select(VPNNode).where(
            VPNNode.id == node_id
        )
    )

    node = result.scalar_one_or_none()

    if not node:
        raise HTTPException(
            status_code=404,
            detail="VPN node not found",
        )

    if not node_accepts_clients(node, management_mode="threexui"):
        raise HTTPException(
            status_code=503,
            detail="VPN node is not available",
        )

    result = await db.execute(
        select(VPNNodeConfig).where(
            VPNNodeConfig.node_id == node_id,
            VPNNodeConfig.protocol == protocol,
        )
    )

    node_config = result.scalar_one_or_none()

    if not node_config:
        raise HTTPException(
            status_code=404,
            detail="VPN node configuration not found",
        )
    if not node_config.config.get("api_address"):
        raise HTTPException(
            status_code=503,
            detail="VPN node has no Xray management address",
        )

    return node, node_config


async def get_active_node_and_config(
    db: AsyncSession,
    protocol: str = "vless",
):
    result = await db.execute(
        select(VPNNode)
        .where(
            VPNNode.status == "active"
        )
        .order_by(VPNNode.id)
    )

    node = next(
        (
            candidate
            for candidate in result.scalars().all()
            if node_accepts_clients(
                candidate, management_mode="threexui"
            )
        ),
        None,
    )

    if not node:
        raise HTTPException(
            status_code=503,
            detail="No active VPN node available",
        )

    result = await db.execute(
        select(VPNNodeConfig).where(
            VPNNodeConfig.node_id == node.id,
            VPNNodeConfig.protocol == protocol,
        )
    )

    node_config = result.scalar_one_or_none()

    if not node_config:
        raise HTTPException(
            status_code=503,
            detail="VPN node configuration not found",
        )
    if not node_config.config.get("api_address"):
        raise HTTPException(
            status_code=503,
            detail="VPN node has no Xray management address",
        )

    return node, node_config


async def add_vless_client_to_xray(
    xray: ThreeXUIClient,
    node_config: VPNNodeConfig,
    client: VPNClient,
) -> None:
    inbound_tag = node_config.config.get(
        "inbound_tag",
        "vless-reality",
    )

    try:
        await xray.add_vless_user(
            inbound_tag=inbound_tag,
            client_uuid=client.client_uuid,
            email=f"vpn-{client.id}",
            flow=client.flow,
        )

    except ThreeXUIError as exc:
        raise HTTPException(
            status_code=502,
            detail=(
                "Failed to add VPN client to Xray: "
                f"{exc}"
            ),
        )


async def remove_vless_client_from_xray(
    xray: ThreeXUIClient,
    node_config: VPNNodeConfig,
    client: VPNClient,
) -> bool:
    """
    Удаляет клиента из Xray.

    True:
        клиент удалён или уже отсутствует.

    False:
        Xray вернул ошибку.
    """

    inbound_tag = node_config.config.get(
        "inbound_tag",
        "vless-reality",
    )

    try:
        await xray.remove_vless_user(
            inbound_tag=inbound_tag,
            email=f"vpn-{client.id}",
        )

        return True

    except ThreeXUIClientNotFound:
        # Идемпотентный delete.
        return True

    except ThreeXUIError:
        return False


# =========================================================
# Create Subscription
# =========================================================


@router.post(
    "",
    response_model=SubscriptionResponse,
)
async def create_subscription(
    data: SubscriptionCreate,
    db: AsyncSession = Depends(get_db),
):
    """Create a subscription and Xray user with compensating rollback."""
    if data.node_id is not None:
        node_id = data.node_id
    else:
        node, _node_config = await get_active_node_and_config(
            db=db, protocol="vless"
        )
        node_id = node.id
    try:
        result = await provision_subscription(
            db,
            user_id=data.user_id,
            plan_id=data.plan_id,
            node_id=node_id,
            client_type=data.client_type,
            flow=data.flow,
            fingerprint=data.fingerprint,
        )
    except ProvisioningNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ProvisioningConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ProvisioningInvalid as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ProvisioningThreeXUIError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    await commit_provisioning(db, result)
    await db.refresh(result.subscription)
    return result.subscription


# =========================================================
# Renew Subscription
# =========================================================


@router.post(
    "/{subscription_id}/renew",
    response_model=SubscriptionResponse,
)
async def renew_subscription(
    subscription_id: int,
    db: AsyncSession = Depends(get_db),
):
    """
    Продлевает существующую подписку.

    Важный принцип:

        subscription
            |
            +-- old client -> revoked
            |
            +-- new client -> active

    Старый VPNClient НЕ переиспользуется.

    Для нового клиента всегда создаётся новый UUID.

    Если подписка ещё active:

        base_date = subscription.expires_at

    Если подписка уже expired:

        base_date = now

    Поэтому:

        active  + renew
            -> продлеваем от текущего expires_at

        expired + renew
            -> начинаем новый период с now
    """

    now = datetime.now(timezone.utc)

    # =====================================================
    # Subscription
    # =====================================================

    result = await db.execute(
        select(Subscription).where(
            Subscription.id == subscription_id
        ).with_for_update()
    )

    subscription = result.scalar_one_or_none()

    if not subscription:
        raise HTTPException(
            status_code=404,
            detail="Subscription not found",
        )

    # =====================================================
    # Plan
    # =====================================================

    plan = await get_plan(
        db=db,
        plan_id=subscription.plan_id,
    )

    # =====================================================
    # User
    # =====================================================

    user = await get_user(
        db=db,
        user_id=subscription.user_id,
    )

    # =====================================================
    # Previous VPN client
    # =====================================================

    result = await db.execute(
        select(VPNClient)
        .where(
            VPNClient.subscription_id == subscription.id
        )
        .order_by(
            VPNClient.id.desc()
        )
        .with_for_update()
    )

    previous_client = result.scalars().first()

    # =====================================================
    # Determine node/protocol
    # =====================================================

    if previous_client:
        protocol = previous_client.protocol
        node_id = previous_client.node_id

        node, node_config = await get_node_for_client(
            db=db,
            node_id=node_id,
            protocol=protocol,
        )

    else:
        # -------------------------------------------------
        # Это возможно для старых подписок,
        # созданных до появления VPNClient.
        # -------------------------------------------------

        protocol = "vless"

        node, node_config = await get_active_node_and_config(
            db=db,
            protocol=protocol,
        )

    # =====================================================
    # Calculate new expiration
    # =====================================================

    if (
        subscription.status == "active"
        and subscription.expires_at > now
    ):
        base_date = subscription.expires_at

    else:
        base_date = now

    new_expires_at = (
        base_date
        + timedelta(days=plan.duration_days)
    )

    # =====================================================
    # Create new VPN client
    # =====================================================

    new_uuid = str(uuid4())

    new_client = VPNClient(
        user_id=user.id,
        subscription_id=subscription.id,
        node_id=node.id,
        protocol=protocol,
        client_type=(previous_client.client_type if previous_client else "universal"),
        flow=(previous_client.flow if previous_client else ""),
        fingerprint=(previous_client.fingerprint if previous_client else "chrome"),
        client_uuid=new_uuid,
        status="provisioning",
        expires_at=new_expires_at,
    )

    db.add(new_client)

    await db.flush()

    # =====================================================
    # Xray
    # =====================================================

    inbound_tag = node_config.config.get(
        "inbound_tag",
        "vless-reality",
    )

    # =====================================================
    # Step 1: Add NEW client to Xray
    # =====================================================

    xray = None
    xray = ThreeXUIClient(address=node_config.config.get("api_address"))
    try:
        await xray.add_vless_user(
            inbound_tag=inbound_tag,
            client_uuid=new_client.client_uuid,
            email=f"vpn-{new_client.id}",
            flow=new_client.flow,
        )

    except ThreeXUIError as exc:
        await db.rollback()

        raise HTTPException(
            status_code=502,
            detail=f"Failed to add renewed VPN client to 3x-ui: {exc}",
        )

    # =====================================================
    # Step 2: Remove OLD client from Xray
    # =====================================================

    if previous_client:
        old_was_active = (
            previous_client.status == "active"
        )

        if old_was_active and xray is not None:
            try:
                await xray.remove_vless_user(
                    inbound_tag=inbound_tag,
                    email=f"vpn-{previous_client.id}",
                )

            except ThreeXUIClientNotFound:
                # Старого клиента уже нет.
                # Это безопасное конечное состояние.
                pass

            except ThreeXUIError as exc:
                # -------------------------------------------------
                # Компенсация:
                #
                # Новый клиент уже был добавлен в Xray.
                # Старого удалить не смогли.
                #
                # Удаляем новый, чтобы не оставить два
                # действующих клиента.
                # -------------------------------------------------

                try:
                    await xray.remove_vless_user(
                        inbound_tag=inbound_tag,
                        email=f"vpn-{new_client.id}",
                    )

                except (
                    ThreeXUIError,
                    ThreeXUIClientNotFound,
                ):
                    pass

                await db.rollback()

                raise HTTPException(
                    status_code=502,
                    detail=(
                        "Failed to remove old VPN client "
                        f"from Xray: {exc}"
                    ),
                )

    # =====================================================
    # Step 3: Update DB state
    # =====================================================

    if previous_client:
        previous_client.status = "revoked"
        previous_client.revoked_at = now

    new_client.status = "active"
    new_client.expires_at = new_expires_at

    subscription.status = "active"
    subscription.starts_at = now
    subscription.expires_at = new_expires_at

    # =====================================================
    # Commit
    # =====================================================

    await db.commit()
    await db.refresh(subscription)

    return subscription


@router.post("/{subscription_id}/rotate", response_model=VPNClientResponse)
async def rotate_subscription_client(
    subscription_id: int,
    data: VPNClientRotate,
    db: AsyncSession = Depends(get_db),
):
    """Reissues a key, optionally moving it to another node, without extending time."""
    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(Subscription)
        .where(Subscription.id == subscription_id)
        .with_for_update()
    )
    subscription = result.scalar_one_or_none()
    if not subscription:
        raise HTTPException(status_code=404, detail="Subscription not found")
    if subscription.status != "active" or subscription.expires_at <= now:
        raise HTTPException(status_code=400, detail="Subscription is not active")
    if data.client_type not in {"amnezia", "universal"}:
        raise HTTPException(status_code=400, detail="Unsupported client type")
    if data.flow not in {"", "xtls-rprx-vision"}:
        raise HTTPException(status_code=400, detail="Unsupported VLESS flow")
    if data.fingerprint not in {"chrome", "firefox", "safari", "randomized"}:
        raise HTTPException(status_code=400, detail="Unsupported fingerprint")

    user = await get_user(db, subscription.user_id)
    target_node, target_config = await get_node_for_client(db, data.node_id, "vless")
    result = await db.execute(
        select(VPNClient)
        .where(VPNClient.subscription_id == subscription.id, VPNClient.status == "active")
        .order_by(VPNClient.id.desc())
        .with_for_update()
    )
    previous = result.scalars().first()

    new_client = VPNClient(
        user_id=user.id,
        subscription_id=subscription.id,
        node_id=target_node.id,
        protocol="vless",
        client_type=data.client_type,
        flow=data.flow,
        fingerprint=data.fingerprint,
        client_uuid=str(uuid4()),
        status="provisioning",
        expires_at=subscription.expires_at,
    )
    db.add(new_client)
    await db.flush()

    target_tag = target_config.config.get("inbound_tag", "vless-reality")
    target_xray = None
    target_xray = ThreeXUIClient(address=target_config.config.get("api_address"))
    try:
        await target_xray.add_vless_user(
            inbound_tag=target_tag,
            client_uuid=new_client.client_uuid,
            email=f"vpn-{new_client.id}",
            flow=new_client.flow,
            expiry_time=int(subscription.expires_at.timestamp() * 1000),
            telegram_id=user.telegram_id,
        )
    except ThreeXUIError as exc:
        await db.rollback()
        raise HTTPException(status_code=502, detail=f"Failed to add replacement client: {exc}")

    if previous and target_xray is not None:
        old_result = await db.execute(
            select(VPNNodeConfig).where(
                VPNNodeConfig.node_id == previous.node_id,
                VPNNodeConfig.protocol == previous.protocol,
            )
        )
        old_config = old_result.scalar_one_or_none()
        old_address = old_config.config.get("api_address") if old_config else ""
        try:
            if not old_config:
                raise ThreeXUIError("Previous node configuration not found")
            if str(old_address).startswith(("http://", "https://")):
                old_xray = ThreeXUIClient(address=old_address)
                await old_xray.remove_vless_user(
                    inbound_tag=old_config.config.get("inbound_tag", "vless-reality"),
                    email=f"vpn-{previous.id}",
                )
        except ThreeXUIClientNotFound:
            pass
        except ThreeXUIError as exc:
            try:
                await target_xray.remove_vless_user(target_tag, f"vpn-{new_client.id}")
            except (ThreeXUIError, ThreeXUIClientNotFound):
                pass
            await db.rollback()
            raise HTTPException(status_code=502, detail=f"Failed to revoke previous client: {exc}")
    if previous:
        previous.status = "revoked"
        previous.revoked_at = now

    new_client.status = "active"
    await db.commit()
    await db.refresh(new_client)
    return new_client
