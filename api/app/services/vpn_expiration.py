from datetime import datetime, timezone
from typing import Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.subscription import Subscription
from app.db.models.vpn_client import VPNClient
from app.db.models.vpn_node_config import VPNNodeConfig
from app.services.threexui import (
    ThreeXUIClient,
    ThreeXUIError,
    ThreeXUIClientNotFound,
)


async def revoke_vpn_client(
    db: AsyncSession,
    client: VPNClient,
    now: datetime,
    *,
    panel_factory: Callable[..., ThreeXUIClient] = ThreeXUIClient,
) -> bool:
    """
    Отзывает VPN-клиента.

    Возможные результаты:

        True:
            - клиент успешно удалён из Xray;
            - клиента уже нет в Xray.

        False:
            - отсутствует конфигурация ноды;
            - Xray недоступен;
            - произошла другая ошибка Xray.

    В случае True клиент переводится в revoked.
    """

    result = await db.execute(
        select(VPNNodeConfig).where(
            VPNNodeConfig.node_id == client.node_id,
            VPNNodeConfig.protocol == client.protocol,
        )
    )

    node_config = result.scalar_one_or_none()

    if not node_config:
        print(
            f"VPN node config not found for client vpn-{client.id}",
            flush=True,
        )
        return False

    api_address = node_config.config.get("api_address")
    if not api_address:
        print(
            f"Xray management address not found for client vpn-{client.id}",
            flush=True,
        )
        return False

    if client.protocol == "vless":
        xray = panel_factory(address=api_address)

        try:
            await xray.remove_vless_user(
                inbound_tag=node_config.config.get(
                    "inbound_tag",
                    "vless-reality",
                ),
                email=f"vpn-{client.id}",
            )

        except ThreeXUIClientNotFound:
            # Идемпотентный revoke:
            # клиента уже нет в Xray, значит с точки зрения
            # конечного состояния всё уже правильно.
            print(
                f"Xray user vpn-{client.id} already absent; "
                "treating revoke as successful",
                flush=True,
            )

        except ThreeXUIError as exc:
            # Xray действительно недоступен или произошла
            # другая ошибка. DB пока не меняем.
            print(
                f"Xray revoke failed for vpn-{client.id}: "
                f"{exc!r}",
                flush=True,
            )
            return False

    else:
        print(
            f"Unsupported VPN protocol for client vpn-{client.id}: "
            f"{client.protocol}",
            flush=True,
        )
        return False

    client.status = "revoked"
    client.revoked_at = now

    return True


async def expire_subscriptions(
    db: AsyncSession,
    *,
    panel_factory: Callable[..., ThreeXUIClient] = ThreeXUIClient,
) -> int:
    """
    Обрабатывает истёкшие подписки.

    Для каждой истёкшей подписки:

        1. Находит активных VPN-клиентов.
        2. Отзывает их из Xray.
        3. Переводит клиентов в revoked.
        4. После успешного revoke всех клиентов
           переводит подписку в expired.

    Если Xray недоступен, подписка остаётся active
    и будет обработана следующим циклом.
    """

    now = datetime.now(timezone.utc)

    result = await db.execute(
        select(Subscription).where(
            Subscription.status == "active",
            Subscription.expires_at <= now,
        )
    )

    subscriptions = result.scalars().all()

    expired_count = 0

    for subscription in subscriptions:
        result = await db.execute(
            select(VPNClient).where(
                VPNClient.subscription_id == subscription.id,
                VPNClient.status == "active",
            )
        )

        clients = result.scalars().all()

        all_clients_revoked = True

        for client in clients:
            revoked = await revoke_vpn_client(
                db=db,
                client=client,
                now=now,
                panel_factory=panel_factory,
            )

            if not revoked:
                all_clients_revoked = False

        if not all_clients_revoked:
            print(
                f"Subscription {subscription.id} "
                "cannot be expired yet: "
                "some VPN clients were not revoked",
                flush=True,
            )
            continue

        subscription.status = "expired"
        expired_count += 1

    if expired_count:
        await db.commit()

    return expired_count


async def expire_vpn_clients(
    db: AsyncSession,
    *,
    panel_factory: Callable[..., ThreeXUIClient] = ThreeXUIClient,
) -> int:
    """
    Дополнительная страховка.

    Отзывает VPN-клиентов, у которых закончился собственный
    expires_at, даже если подписка всё ещё active.

    Это позволяет отдельно обрабатывать ситуацию:

        subscription = active
        client = expired
    """

    now = datetime.now(timezone.utc)

    result = await db.execute(
        select(VPNClient).where(
            VPNClient.status == "active",
            VPNClient.expires_at <= now,
        )
    )

    clients = result.scalars().all()

    expired_count = 0

    for client in clients:
        revoked = await revoke_vpn_client(
            db=db,
            client=client,
            now=now,
            panel_factory=panel_factory,
        )

        if revoked:
            expired_count += 1

    if expired_count:
        await db.commit()

    return expired_count


async def expire_desired_state(db: AsyncSession) -> tuple[int, int]:
    """Expire DB state when Xray is managed by outbound node agents.

    The node agent treats active DB clients as the desired state. Marking an
    expired client revoked therefore removes it from the next state response;
    the agent performs the actual Xray removal and reports its result.
    """

    now = datetime.now(timezone.utc)
    client_result = await db.execute(
        select(VPNClient).where(
            VPNClient.status == "active",
            VPNClient.expires_at <= now,
        )
    )
    clients = client_result.scalars().all()
    for client in clients:
        client.status = "revoked"
        client.revoked_at = now

    subscription_result = await db.execute(
        select(Subscription).where(
            Subscription.status == "active",
            Subscription.expires_at <= now,
        )
    )
    subscriptions = subscription_result.scalars().all()
    subscription_ids = [subscription.id for subscription in subscriptions]
    if subscription_ids:
        remaining_result = await db.execute(
            select(VPNClient).where(
                VPNClient.subscription_id.in_(subscription_ids),
                VPNClient.status == "active",
            )
        )
        remaining = remaining_result.scalars().all()
        for client in remaining:
            client.status = "revoked"
            client.revoked_at = now
        clients.extend(client for client in remaining if client not in clients)
    for subscription in subscriptions:
        subscription.status = "expired"

    if clients or subscriptions:
        await db.commit()
    return len(subscriptions), len(clients)
