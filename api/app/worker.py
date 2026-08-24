import asyncio
import logging

from sqlalchemy import text

from app.core.config import settings
from app.core.logging import configure_logging
from app.db.session import AsyncSessionLocal, engine
from app.services.audit import write_audit
from app.services.lifecycle import run_lifecycle_once


configure_logging(settings.log_level)
logger = logging.getLogger(__name__)


async def run_locked_cycle() -> bool:
    async with engine.connect() as lock_connection:
        acquired = await lock_connection.scalar(
            text("SELECT pg_try_advisory_lock(:key)"),
            {"key": settings.lifecycle_advisory_lock_key},
        )
        if not acquired:
            logger.info("lifecycle_lock_busy", extra={"event": {"event_type": "lifecycle_lock_busy"}})
            return False
        try:
            async with AsyncSessionLocal() as db:
                result = await run_lifecycle_once(db)
                errors = sum(item.errors for item in result.reconciliation)
                restored = sum(item.restored for item in result.reconciliation)
                removed = sum(item.removed for item in result.reconciliation)
                event = {
                    "event_type": "lifecycle_cycle",
                    "expired_subscriptions": result.expired_subscriptions,
                    "revoked_clients": result.revoked_clients,
                    "expired_debug_sessions": result.expired_debug_sessions,
                    "xray_restored": restored,
                    "xray_removed": removed,
                    "xray_errors": errors,
                }
                logger.info("lifecycle_cycle", extra={"event": event})
                await write_audit(
                    db,
                    action="lifecycle.cycle",
                    result="failure" if errors else "success",
                    resource_type="worker",
                    details=event,
                )
            return True
        finally:
            await lock_connection.execute(
                text("SELECT pg_advisory_unlock(:key)"),
                {"key": settings.lifecycle_advisory_lock_key},
            )


async def main() -> None:
    logger.info("lifecycle_worker_started", extra={"event": {"event_type": "worker_start"}})
    if settings.worker_run_once:
        await run_locked_cycle()
        return
    while True:
        try:
            await run_locked_cycle()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("lifecycle worker cycle failed")
        await asyncio.sleep(settings.lifecycle_interval_seconds)


if __name__ == "__main__":
    asyncio.run(main())
