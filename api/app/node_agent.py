import asyncio
import logging
import os
import re
import time

import httpx

from app.core.logging import configure_logging
from app.services.xray import XrayClient, XrayError, XrayUserAlreadyExists, XrayUserNotFound


configure_logging(os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)
MANAGED_EMAIL = re.compile(r"^vpn-\d+$")


async def reconcile_once() -> dict:
    control_plane = os.environ["CONTROL_PLANE_URL"].rstrip("/")
    token = os.environ["NODE_AGENT_TOKEN"]
    address = os.getenv("NODE_XRAY_API_ADDRESS", "172.18.0.1:10085")
    headers = {"Authorization": f"Bearer {token}"}
    started = time.perf_counter()
    async with httpx.AsyncClient(base_url=control_plane, headers=headers, timeout=15.0) as client:
        response = await client.get("/agent/v1/state")
        response.raise_for_status()
        state = response.json()
        inbound_tag = state["inbound_tag"]
        xray = XrayClient(address=address)
        try:
            actual_users = await xray.get_users(inbound_tag)
        except XrayError as exc:
            report = {
                "status": "offline",
                "latency_ms": round((time.perf_counter() - started) * 1000, 2),
                "xray_users": 0,
                "active_connections": 0,
                "restored": 0,
                "removed": 0,
                "errors": [str(exc)],
            }
            status_response = await client.post("/agent/v1/status", json=report)
            status_response.raise_for_status()
            logger.error(
                "node_reconciliation_failed",
                extra={"event": {"event_type": "node_reconciliation", **report}},
            )
            return report
        actual = {user.email for user in actual_users if user.email}
        expected = {item["email"]: item for item in state["clients"]}
        restored = 0
        removed = 0
        errors: list[str] = []
        for email, item in expected.items():
            if email in actual:
                continue
            try:
                await xray.add_vless_user(
                    inbound_tag=inbound_tag,
                    client_uuid=item["client_uuid"],
                    email=email,
                    flow=item["flow"],
                )
                restored += 1
            except XrayUserAlreadyExists:
                pass
            except XrayError as exc:
                errors.append(str(exc))
        for email in actual:
            if not MANAGED_EMAIL.fullmatch(email) or email in expected:
                continue
            try:
                await xray.remove_vless_user(inbound_tag=inbound_tag, email=email)
                removed += 1
            except XrayUserNotFound:
                pass
            except XrayError as exc:
                errors.append(str(exc))
        latency_ms = round((time.perf_counter() - started) * 1000, 2)
        status = "online" if not errors else "degraded"
        report = {
            "status": status,
            "latency_ms": latency_ms,
            "xray_users": len(actual) + restored - removed,
            "active_connections": 0,
            "restored": restored,
            "removed": removed,
            "errors": errors[:50],
        }
        status_response = await client.post("/agent/v1/status", json=report)
        status_response.raise_for_status()
        logger.info("node_reconciliation", extra={"event": {"event_type": "node_reconciliation", **report}})
        return report


async def main() -> None:
    interval = int(os.getenv("NODE_AGENT_INTERVAL_SECONDS", "30"))
    logger.info("node_agent_started", extra={"event": {"event_type": "node_agent_start"}})
    while True:
        try:
            await reconcile_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("node agent reconciliation failed")
        await asyncio.sleep(interval)


if __name__ == "__main__":
    asyncio.run(main())
