import asyncio
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from app.core.logging import configure_logging
from app.services.xray import XrayClient, XrayError, XrayUserAlreadyExists, XrayUserNotFound


configure_logging(os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)
MANAGED_EMAIL = re.compile(r"^vpn-\d+$")
ACTIVITY_LINE = re.compile(
    r"^(?P<timestamp>\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?) "
    r"from (?P<source>\S+) accepted .* email: vpn-(?P<client_id>\d+)$"
)
_activity_inode: int | None = None
_activity_offset = 0


def _source_ip(value: str) -> str:
    if value.startswith("[") and "]" in value:
        return value[1 : value.index("]")]
    host, separator, _ = value.rpartition(":")
    return host if separator else value


def collect_client_activity(path: str) -> list[dict]:
    global _activity_inode, _activity_offset
    log_path = Path(path)
    try:
        stat = log_path.stat()
        if _activity_inode != stat.st_ino or stat.st_size < _activity_offset:
            _activity_inode = stat.st_ino
            _activity_offset = 0
        with log_path.open("r", encoding="utf-8", errors="replace") as handle:
            handle.seek(_activity_offset)
            lines = handle.readlines()
            _activity_offset = handle.tell()
    except (FileNotFoundError, PermissionError, OSError):
        return []

    latest: dict[int, dict] = {}
    for line in lines:
        match = ACTIVITY_LINE.match(line.strip())
        if match is None:
            continue
        raw_timestamp = match.group("timestamp")
        timestamp_format = "%Y/%m/%d %H:%M:%S.%f" if "." in raw_timestamp else "%Y/%m/%d %H:%M:%S"
        connected_at = datetime.strptime(raw_timestamp, timestamp_format).replace(
            tzinfo=timezone.utc
        )
        client_id = int(match.group("client_id"))
        latest[client_id] = {
            "client_id": client_id,
            "connected_at": connected_at.isoformat(),
            "ip_address": _source_ip(match.group("source")),
        }
    return list(latest.values())


async def reconcile_once() -> dict:
    control_plane = os.environ["CONTROL_PLANE_URL"].rstrip("/")
    token = os.environ["NODE_AGENT_TOKEN"]
    address = os.getenv("NODE_XRAY_API_ADDRESS", "172.18.0.1:10085")
    headers = {"Authorization": f"Bearer {token}"}
    access_log = os.getenv("NODE_XRAY_ACCESS_LOG", "/var/log/xray/access.log")
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
                "activities": collect_client_activity(access_log),
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
            "activities": collect_client_activity(access_log),
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
