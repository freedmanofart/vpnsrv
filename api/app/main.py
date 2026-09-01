import asyncio
import logging
import time
from pathlib import Path
from uuid import uuid4
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes.plans import router as plans_router
from app.api.routes.users import router as users_router
from app.api.routes.user_status import router as user_status_router
from app.api.routes.vpn import router as vpn_router
from app.api.routes.subscriptions import router as subscriptions_router
from app.api.routes.admin import router as admin_router
from app.api.routes.payments import router as payments_router
from app.api.routes.client import router as client_router
from app.api.routes.payment_methods import router as payment_methods_router
from app.api.routes.web import router as web_router

from app.db.session import get_db, AsyncSessionLocal

from app.services.lifecycle import run_lifecycle_once
from app.core.security import require_api_access
from app.core.config import settings
from app.core.logging import configure_logging, request_id_context
from app.services.audit import sensitive_debug_active, write_audit


logger = logging.getLogger(__name__)
configure_logging(settings.log_level)


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
                result = await run_lifecycle_once(db)

            subscription_count = result.expired_subscriptions
            client_count = result.revoked_clients
            reconciliation = result.reconciliation

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

        await asyncio.sleep(settings.lifecycle_interval_seconds)


@asynccontextmanager
async def lifespan(app: FastAPI):
    print(
        "LIFESPAN STARTED",
        flush=True,
    )

    task = asyncio.create_task(vpn_expiration_loop()) if settings.background_jobs_enabled else None

    try:
        yield

    finally:
        print(
            "LIFESPAN STOPPING",
            flush=True,
        )

        if task is not None:
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
app.mount(
    "/static",
    StaticFiles(directory=Path(__file__).resolve().parent / "static"),
    name="static",
)


@app.middleware("http")
async def request_context_and_audit(request: Request, call_next):
    request_id = request.headers.get("X-Request-Id") or uuid4().hex
    context_token = request_id_context.set(request_id)
    started = time.perf_counter()
    response = None
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        response.headers["X-Request-Id"] = request_id
        return response
    finally:
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        logged_path = request.url.path
        if logged_path.startswith("/cabinet/access/"):
            logged_path = "/cabinet/access/[redacted]"
        logger.info(
            "http_request",
            extra={
                "event": {
                    "event_type": "http_request",
                    "method": request.method,
                    "path": logged_path,
                    "status_code": status_code,
                    "duration_ms": duration_ms,
                    "client_ip": request.client.host if request.client else None,
                }
            },
        )
        principal = getattr(request.state, "principal", None)
        try:
            async with AsyncSessionLocal() as audit_db:
                should_audit = request.method in {"POST", "PUT", "PATCH", "DELETE"}
                if not should_audit and request.headers.get("authorization"):
                    should_audit = await sensitive_debug_active(audit_db)
                if should_audit:
                    await write_audit(
                        audit_db,
                        action=f"http.{request.method.lower()}",
                        result="success" if status_code < 400 else "failure",
                        actor_type=getattr(principal, "kind", "anonymous"),
                        actor_id=getattr(principal, "name", None),
                        resource_type="http",
                        resource_id=logged_path,
                        ip_address=request.client.host if request.client else None,
                        details={"status_code": status_code, "duration_ms": duration_ms},
                        sensitive_details={
                            "authorization": request.headers.get("authorization")
                        },
                    )
        except Exception:
            logger.exception("request audit failed")
        request_id_context.reset(context_token)


app.include_router(users_router)
app.include_router(user_status_router)
app.include_router(plans_router)
app.include_router(subscriptions_router)
app.include_router(vpn_router)
app.include_router(admin_router)
app.include_router(payments_router)
app.include_router(client_router)
app.include_router(payment_methods_router)
app.include_router(web_router)


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
