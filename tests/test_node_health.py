from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from api.app.services.node_health import effective_node_health, node_accepts_clients


def test_agent_node_with_stale_heartbeat_is_offline() -> None:
    now = datetime.now(timezone.utc)
    assert effective_node_health(
        "online",
        now - timedelta(minutes=2),
        management_mode="agent",
        now=now,
    ) == "offline"


def test_fresh_agent_node_accepts_clients() -> None:
    node = SimpleNamespace(
        status="active",
        health_status="online",
        last_seen_at=datetime.now(timezone.utc),
        active_connections=2,
        capacity=100,
    )
    assert node_accepts_clients(node, management_mode="agent")


def test_unknown_agent_node_does_not_accept_clients() -> None:
    node = SimpleNamespace(
        status="active",
        health_status="unknown",
        last_seen_at=None,
        active_connections=0,
        capacity=100,
    )
    assert not node_accepts_clients(node, management_mode="agent")


def test_direct_mode_keeps_legacy_health_behavior() -> None:
    node = SimpleNamespace(
        status="active",
        health_status="unknown",
        last_seen_at=None,
        active_connections=0,
        capacity=100,
    )
    assert node_accepts_clients(node, management_mode="direct")
