from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.services.audit import expire_debug_sessions
from app.services.reconciliation import ReconciliationReport, reconcile_all_nodes
from app.services.vpn_expiration import (
    expire_desired_state,
    expire_subscriptions,
    expire_vpn_clients,
)


@dataclass
class LifecycleResult:
    expired_subscriptions: int
    revoked_clients: int
    expired_debug_sessions: int
    reconciliation: list[ReconciliationReport]


async def run_lifecycle_once(db: AsyncSession) -> LifecycleResult:
    if settings.xray_management_mode == "agent":
        expired_subscriptions, revoked_clients = await expire_desired_state(db)
        reconciliation = []
    elif settings.xray_management_mode == "direct":
        expired_subscriptions = await expire_subscriptions(db)
        revoked_clients = await expire_vpn_clients(db)
        reconciliation = await reconcile_all_nodes(db)
    else:
        raise ValueError(
            "XRAY_MANAGEMENT_MODE must be either 'direct' or 'agent'"
        )
    expired_debug = await expire_debug_sessions(db)
    if expired_debug:
        await db.commit()
    return LifecycleResult(
        expired_subscriptions=expired_subscriptions,
        revoked_clients=revoked_clients,
        expired_debug_sessions=expired_debug,
        reconciliation=reconciliation,
    )
