import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes.plans import router as plans_router
from app.api.routes.users import router as users_router
from app.api.routes.user_status import router as user_status_router
from app.api.routes.vpn import router as vpn_router
from app.api.routes.subscriptions import router as subscriptions_router
from app.api.routes.admin import router as admin_router
from app.api.routes.payments import router as payments_router

from app.db.session import get_db, AsyncSessionLocal

from app.services.vpn_expiration import (
    expire_subscriptions,
    expire_vpn_clients,
)
from app.services.reconciliation import reconcile_all_nodes
from app.core.security import require_api_access


logger = logging.getLogger(__name__)


async def vpn_expiration_loop():
    print(
        "VPN EXPIRATION LOOP STARTED",
        flush=True,
    )

    while True:
        try:
            print(
                "VPN EXPIRATION CHECK",
                flush=True,
            )

            async with AsyncSessionLocal() as db:
                subscription_count = await expire_subscriptions(db)
                client_count = await expire_vpn_clients(db)
                reconciliation = await reconcile_all_nodes(db)

            if subscription_count:
                logger.info(
                    "Subscription expiration: expired %s subscription(s)",
                    subscription_count,
                )

            if client_count:
                logger.info(
                    "VPN expiration: revoked %s client(s)",
                    client_count,
                )

            reconciliation_errors = sum(item.errors for item in reconciliation)
            reconciliation_restored = sum(item.restored for item in reconciliation)
            reconciliation_removed = sum(item.removed for item in reconciliation)
            if reconciliation_errors or reconciliation_restored or reconciliation_removed:
                logger.info(
                    "Xray reconciliation: restored=%s removed=%s errors=%s",
                    reconciliation_restored,
                    reconciliation_removed,
                    reconciliation_errors,
                )

            print(
                "VPN EXPIRATION RESULT: "
                f"subscriptions={subscription_count}, "
                f"clients={client_count}",
                flush=True,
            )

        except asyncio.CancelledError:
            raise

        except Exception as exc:
            print(
                f"VPN EXPIRATION ERROR: {exc!r}",
                flush=True,
            )

            logger.exception(
                "VPN expiration job failed"
            )

        await asyncio.sleep(60)


@asynccontextmanager
async def lifespan(app: FastAPI):
    print(
        "LIFESPAN STARTED",
        flush=True,
    )

    task = asyncio.create_task(
        vpn_expiration_loop()
    )

    try:
        yield

    finally:
        print(
            "LIFESPAN STOPPING",
            flush=True,
        )

        task.cancel()

        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(
    title="VPN API",
    version="0.2.0",
    lifespan=lifespan,
)


app.include_router(users_router)
app.include_router(user_status_router)
app.include_router(plans_router)
app.include_router(subscriptions_router)
app.include_router(vpn_router)
app.include_router(admin_router)
app.include_router(payments_router)


@app.get("/health")
async def health():
    return {
        "status": "ok"
    }


@app.get("/db-health")
async def db_health(
    db: AsyncSession = Depends(get_db),
    _principal=Depends(require_api_access),
):
    result = await db.execute(
        text("SELECT 1")
    )

    return {
        "database": result.scalar_one() == 1
    }


@app.get("/")
async def root():
    return {
        "service": "vpn-api",
        "version": "0.2.0",
    }
