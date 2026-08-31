from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.audit import expire_debug_sessions
from app.services.reconciliation import ReconciliationReport, reconcile_all_nodes
from app.services.vpn_expiration import (
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
    expired_subscriptions = await expire_subscriptions(db)
    revoked_clients = await expire_vpn_clients(db)
    reconciliation = await reconcile_all_nodes(db)
    expired_debug = await expire_debug_sessions(db)
    if expired_debug:
        await db.commit()
    return LifecycleResult(
        expired_subscriptions=expired_subscriptions,
        revoked_clients=revoked_clients,
        expired_debug_sessions=expired_debug,
        reconciliation=reconciliation,
    )
