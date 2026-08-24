from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.user import User
from app.db.models.plan import Plan
from app.db.models.subscription import Subscription
from app.db.models.vpn_client import VPNClient
from app.db.models.vpn_node import VPNNode
from app.db.models.vpn_node_config import VPNNodeConfig

from app.db.session import get_db

from app.services.xray import (
    XrayClient,
    XrayError,
    XrayUserNotFound,
)

from app.schemas.subscription import (
    SubscriptionCreate,
    SubscriptionResponse,
)


router = APIRouter(
    prefix="/subscriptions",
    tags=["Subscriptions"],
)


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

    if node.status != "active":
        raise HTTPException(
            status_code=400,
            detail="VPN node is not active",
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

    node = result.scalars().first()

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

    return node, node_config


async def add_vless_client_to_xray(
    xray: XrayClient,
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
        )

    except XrayError as exc:
        raise HTTPException(
            status_code=502,
            detail=(
                "Failed to add VPN client to Xray: "
                f"{exc}"
            ),
        )


async def remove_vless_client_from_xray(
    xray: XrayClient,
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

    except XrayUserNotFound:
        # Идемпотентный delete.
        return True

    except XrayError:
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
    """
    Создаёт новую подписку и VPN-клиента.

    Последовательность:

        1. Проверяем user.
        2. Проверяем plan.
        3. Проверяем отсутствие активной подписки.
        4. Находим активную VPN-ноду.
        5. Создаём subscription.
        6. Создаём VPN client.
        7. Добавляем client в Xray.
        8. Commit.

    Если Xray недоступен:
        subscription/client в БД не сохраняются.
    """

    # -----------------------------------------------------
    # User
    # -----------------------------------------------------

    user = await get_user(
        db=db,
        user_id=data.user_id,
    )

    # -----------------------------------------------------
    # Plan
    # -----------------------------------------------------

    plan = await get_plan(
        db=db,
        plan_id=data.plan_id,
    )

    # -----------------------------------------------------
    # Existing active subscription
    # -----------------------------------------------------

    now = datetime.now(timezone.utc)

    result = await db.execute(
        select(Subscription).where(
            Subscription.user_id == data.user_id,
            Subscription.status == "active",
            Subscription.expires_at > now,
        )
    )

    existing = result.scalar_one_or_none()

    if existing:
        raise HTTPException(
            status_code=409,
            detail="Active subscription already exists",
        )

    # -----------------------------------------------------
    # VPN node
    # -----------------------------------------------------

    node, node_config = await get_active_node_and_config(
        db=db,
        protocol="vless",
    )

    # -----------------------------------------------------
    # Subscription
    # -----------------------------------------------------

    expires_at = (
        now
        + timedelta(days=plan.duration_days)
    )

    subscription = Subscription(
        user_id=user.id,
        plan_id=plan.id,
        status="active",
        starts_at=now,
        expires_at=expires_at,
    )

    db.add(subscription)

    await db.flush()

    # -----------------------------------------------------
    # VPN client
    # -----------------------------------------------------

    client_uuid = str(uuid4())

    client = VPNClient(
        user_id=user.id,
        subscription_id=subscription.id,
        node_id=node.id,
        protocol="vless",
        client_uuid=client_uuid,
        status="active",
        expires_at=expires_at,
    )

    db.add(client)

    await db.flush()

    # -----------------------------------------------------
    # Xray
    # -----------------------------------------------------

    xray = XrayClient()

    try:
        await add_vless_client_to_xray(
            xray=xray,
            node_config=node_config,
            client=client,
        )

    except HTTPException:
        await db.rollback()
        raise

    # -----------------------------------------------------
    # Commit
    # -----------------------------------------------------

    await db.commit()
    await db.refresh(subscription)

    return subscription


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
        )
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
        client_uuid=new_uuid,
        status="active",
        expires_at=new_expires_at,
    )

    db.add(new_client)

    await db.flush()

    # =====================================================
    # Xray
    # =====================================================

    xray = XrayClient()

    inbound_tag = node_config.config.get(
        "inbound_tag",
        "vless-reality",
    )

    # =====================================================
    # Step 1: Add NEW client to Xray
    # =====================================================

    try:
        await xray.add_vless_user(
            inbound_tag=inbound_tag,
            client_uuid=new_client.client_uuid,
            email=f"vpn-{new_client.id}",
        )

    except XrayError as exc:
        await db.rollback()

        raise HTTPException(
            status_code=502,
            detail=(
                "Failed to add renewed VPN client "
                f"to Xray: {exc}"
            ),
        )

    # =====================================================
    # Step 2: Remove OLD client from Xray
    # =====================================================

    if previous_client:
        old_was_active = (
            previous_client.status == "active"
        )

        if old_was_active:
            try:
                await xray.remove_vless_user(
                    inbound_tag=inbound_tag,
                    email=f"vpn-{previous_client.id}",
                )

            except XrayUserNotFound:
                # Старого клиента уже нет.
                # Это безопасное конечное состояние.
                pass

            except XrayError as exc:
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
                    XrayError,
                    XrayUserNotFound,
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
