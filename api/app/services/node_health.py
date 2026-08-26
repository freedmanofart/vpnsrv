from datetime import datetime, timedelta, timezone


AGENT_HEARTBEAT_MAX_AGE = timedelta(seconds=90)


def effective_node_health(
    health_status: str,
    last_seen_at: datetime | None,
    *,
    management_mode: str,
    now: datetime | None = None,
) -> str:
    """Не считать ноду доступной после прекращения heartbeat от node-agent."""
    if management_mode != "agent" or health_status != "online":
        return health_status
    if last_seen_at is None:
        return "offline"
    current = now or datetime.now(timezone.utc)
    seen = last_seen_at if last_seen_at.tzinfo else last_seen_at.replace(tzinfo=timezone.utc)
    return "offline" if current - seen > AGENT_HEARTBEAT_MAX_AGE else "online"


def node_accepts_clients(node, *, management_mode: str) -> bool:
    """Проверить административное состояние, heartbeat и свободную ёмкость."""
    health = effective_node_health(
        node.health_status,
        node.last_seen_at,
        management_mode=management_mode,
    )
    return (
        node.status == "active"
        and (management_mode != "agent" or health == "online")
        and node.active_connections < node.capacity
    )
