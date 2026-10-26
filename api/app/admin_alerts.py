"""Periodic delivery of operator health transitions and audit events to Telegram."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from datetime import datetime, timezone

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes.admin import health_dashboard
from app.core.config import settings
from app.db.models.admin_setting import AdminSetting
from app.db.models.audit import AuditLog
from app.db.session import AsyncSessionLocal, engine
from app.services.notifications import send_admin_telegram_message


logger = logging.getLogger(__name__)
HEALTH_KEY_PREFIX = "admin_alert.health."
HEALTH_PENDING_KEY_PREFIX = "admin_alert.health_pending."
AUDIT_CURSOR_KEY = "admin_alert.audit_cursor"
ADVISORY_LOCK_KEY = 846202607
MAX_AUDIT_ROWS_PER_PASS = 100
SUPPRESSED_AUDIT_ACTIONS = frozenset({"lifecycle.cycle"})
HEALTH_TRANSITION_CONFIRMATIONS = 2


def _admin_dashboard_link() -> str:
    return f"{settings.public_base_url.rstrip('/')}/admin"


def _clean(value: object, limit: int = 350) -> str:
    text_value = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value or ""))
    return " ".join(text_value.split())[:limit]


def _health_key(check: dict) -> str:
    identity = str(check.get("id") or check.get("name") or "unknown")
    return HEALTH_KEY_PREFIX + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]


async def _setting(db: AsyncSession, key: str) -> AdminSetting | None:
    return await db.get(AdminSetting, key)


async def _save_setting(db: AsyncSession, key: str, value: str) -> None:
    item = await _setting(db, key)
    if item is None:
        db.add(AdminSetting(key=key, value=value))
    else:
        item.value = value


def _health_alert_lines(changes: list[tuple[dict, str | None]]) -> list[str]:
    lines = []
    for check, old_status in changes:
        status = str(check.get("status") or "degraded")
        name = _clean(check.get("name") or check.get("id") or "Health check", 160)
        details = _clean(check.get("details") or "", 430)
        if old_status is None:
            transition = f"первый статус: {status.upper()}"
        else:
            transition = f"{old_status.upper()} → {status.upper()}"
        line = f"• {name}: {transition}"
        if details:
            line += f" — {details}"
        lines.append(line)
    return lines


def _split_message(title: str, lines: list[str], max_chars: int = 3900) -> list[str]:
    if not lines:
        return []
    result: list[str] = []
    current = title
    for line in lines:
        if len(current) + len(line) + 1 > max_chars and current != title:
            result.append(current)
            current = title + " (продолжение)"
        current += "\n" + line
    result.append(current)
    return result


async def notify_health_transitions(db: AsyncSession, checks: list[dict]) -> None:
    changes: list[tuple[dict, str | None]] = []
    state_updates: dict[str, str] = {}
    pending_updates: dict[str, str | None] = {}
    for check in checks:
        status = str(check.get("status") or "degraded")
        if status not in {"online", "degraded", "offline"}:
            status = "degraded"
            check = {**check, "status": status}
        key = _health_key(check)
        pending_key = HEALTH_PENDING_KEY_PREFIX + key.removeprefix(HEALTH_KEY_PREFIX)
        previous = await _setting(db, key)
        old_status = previous.value if previous is not None else None
        pending = await _setting(db, pending_key)

        # Per-relay and optional diagnostic checks remain visible in the health
        # dashboard, but their grouped check is the one that drives Telegram.
        if check.get("alert") is False:
            if old_status != status:
                state_updates[key] = status
            pending_updates[pending_key] = None
            continue

        if old_status == status:
            pending_updates[pending_key] = None
            continue

        pending_count = 0
        sample_marker = check.get("generated_at")
        if hasattr(sample_marker, "isoformat"):
            sample_marker = sample_marker.isoformat()
        elif sample_marker is not None:
            sample_marker = str(sample_marker)
        pending_data = {}
        if pending is not None:
            try:
                pending_data = json.loads(pending.value)
                if isinstance(pending_data, dict) and pending_data.get("status") == status:
                    pending_count = max(0, int(pending_data.get("count", 0)))
            except (ValueError, TypeError, json.JSONDecodeError):
                pending_data = {}
                pending_count = 0
        if not (
            pending_count > 0
            and sample_marker is not None
            and pending_data.get("sample_marker") == sample_marker
        ):
            pending_count += 1

        # A first online sample establishes the baseline silently. Every
        # failure, degradation, or recovery transition needs two matching
        # dashboard samples so a single timeout cannot page the operators.
        if old_status is None and status == "online":
            state_updates[key] = status
            pending_updates[pending_key] = None
        elif pending_count < HEALTH_TRANSITION_CONFIRMATIONS:
            pending_updates[pending_key] = json.dumps(
                {"status": status, "count": pending_count, "sample_marker": sample_marker},
                separators=(",", ":"),
            )
        else:
            changes.append((check, old_status))
            state_updates[key] = status
            pending_updates[pending_key] = None

    if changes:
        messages = _split_message("🚨 Freedom VPN: изменение health-статуса", _health_alert_lines(changes))
        if messages:
            messages[0] += f"\n\nАдминка: {_admin_dashboard_link()}"
        delivered = True
        for message in messages:
            if not await send_admin_telegram_message(db, message):
                delivered = False
                break
        if not delivered:
            # Keep confirmed transitions pending so the next timer run retries
            # delivery instead of losing an alert.
            for check, _ in changes:
                key = _health_key(check)
                state_updates.pop(key, None)
                pending_key = HEALTH_PENDING_KEY_PREFIX + key.removeprefix(HEALTH_KEY_PREFIX)
                pending_updates[pending_key] = json.dumps(
                    {
                        "status": check.get("status") or "degraded",
                        "count": HEALTH_TRANSITION_CONFIRMATIONS,
                        "sample_marker": (
                            check.get("generated_at").isoformat()
                            if hasattr(check.get("generated_at"), "isoformat")
                            else check.get("generated_at")
                        ),
                    },
                    separators=(",", ":"),
                )

    for key, status in state_updates.items():
        await _save_setting(db, key, status)
    for key, value in pending_updates.items():
        if value is None:
            item = await _setting(db, key)
            if item is not None:
                await db.delete(item)
        else:
            await _save_setting(db, key, value)
    if state_updates or pending_updates:
        await db.commit()


def _audit_event_line(row: AuditLog) -> str:
    created = row.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    actor = _clean(row.actor_id or row.actor_type, 100)
    resource = ""
    if row.resource_type:
        resource = _clean(row.resource_type, 60)
        if row.resource_id:
            resource += "#" + _clean(row.resource_id, 70)
    node = f" node#{row.node_id}" if row.node_id is not None else ""
    suffix = f"; {resource}" if resource else ""
    return (
        f"• {created.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')} "
        f"{_clean(row.action, 128)} → {_clean(row.result, 32)}; actor={actor}"
        f"{suffix}{node}"
    )


async def _initialize_audit_cursor(db: AsyncSession) -> int:
    cursor = await _setting(db, AUDIT_CURSOR_KEY)
    if cursor is not None:
        try:
            return max(0, int(cursor.value))
        except ValueError:
            pass
    latest = int(await db.scalar(select(func.max(AuditLog.id))) or 0)
    await _save_setting(db, AUDIT_CURSOR_KEY, str(latest))
    await db.commit()
    return latest


async def notify_audit_events(db: AsyncSession) -> int:
    cursor = await _initialize_audit_cursor(db)
    rows = list(
        (
            await db.scalars(
                select(AuditLog)
                .where(AuditLog.id > cursor)
                .order_by(AuditLog.id.asc())
                .limit(MAX_AUDIT_ROWS_PER_PASS)
            )
        ).all()
    )
    if not rows:
        return 0

    # Worker lifecycle cycles are frequent operational heartbeats. Keep them
    # in the audit log, but do not send them to the administrator's Telegram.
    notification_rows = [row for row in rows if row.action not in SUPPRESSED_AUDIT_ACTIONS]
    if notification_rows:
        lines = [_audit_event_line(row) for row in notification_rows]
        messages = _split_message("📋 Freedom VPN: события сервиса", lines, max_chars=3900)
        if messages:
            messages[0] += f"\n\nАдминка: {_admin_dashboard_link()}"
        for message in messages:
            if not await send_admin_telegram_message(db, message):
                logger.error("admin_audit_alert_delivery_failed cursor=%s", cursor)
                return 0

    latest_id = rows[-1].id
    await _save_setting(db, AUDIT_CURSOR_KEY, str(latest_id))
    await db.commit()
    return len(notification_rows)


async def run_once() -> bool:
    async with engine.connect() as lock_connection:
        acquired = await lock_connection.scalar(
            text("SELECT pg_try_advisory_lock(:key)"), {"key": ADVISORY_LOCK_KEY}
        )
        if not acquired:
            logger.info("admin_alerts_lock_busy")
            return False
        try:
            async with AsyncSessionLocal() as db:
                dashboard = await health_dashboard(db)
                await notify_health_transitions(db, dashboard.get("checks") or [])
                event_count = await notify_audit_events(db)
                logger.info("admin_alerts_check_complete events=%s", event_count)
            return True
        finally:
            await lock_connection.execute(
                text("SELECT pg_advisory_unlock(:key)"), {"key": ADVISORY_LOCK_KEY}
            )


async def main() -> None:
    await run_once()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    # httpx logs full request URLs at INFO, which would expose the Telegram bot
    # token embedded in the Bot API URL in the system journal.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    asyncio.run(main())
