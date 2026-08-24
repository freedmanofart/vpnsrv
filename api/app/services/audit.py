from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import redact, request_id_context
from app.db.models.audit import AuditLog, DebugSession


async def expire_debug_sessions(db: AsyncSession) -> int:
    now = datetime.now(timezone.utc)
    result = await db.execute(
        update(DebugSession)
        .where(DebugSession.status == "active", DebugSession.expires_at <= now)
        .values(status="expired", closed_at=now)
    )
    return result.rowcount or 0


async def sensitive_debug_active(db: AsyncSession) -> bool:
    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(DebugSession.id).where(
            DebugSession.status == "active",
            DebugSession.expires_at > now,
        ).limit(1)
    )
    return result.scalar_one_or_none() is not None


async def write_audit(
    db: AsyncSession,
    *,
    action: str,
    result: str,
    actor_type: str = "system",
    actor_id: str | None = None,
    resource_type: str | None = None,
    resource_id: str | int | None = None,
    node_id: int | None = None,
    ip_address: str | None = None,
    details: dict[str, Any] | None = None,
    sensitive_details: dict[str, Any] | None = None,
    commit: bool = True,
) -> AuditLog:
    sensitive = bool(sensitive_details) and await sensitive_debug_active(db)
    stored_details = dict(details or {})
    stored_details.update(sensitive_details or {})
    if not sensitive:
        stored_details = redact(stored_details)
    entry = AuditLog(
        request_id=request_id_context.get(),
        actor_type=actor_type,
        actor_id=actor_id,
        action=action,
        resource_type=resource_type,
        resource_id=str(resource_id) if resource_id is not None else None,
        result=result,
        node_id=node_id,
        ip_address=ip_address,
        details=stored_details,
        sensitive=sensitive,
    )
    db.add(entry)
    if commit:
        await db.commit()
        await db.refresh(entry)
    return entry
