#!/usr/bin/env python3
"""Check real VPN traffic counters for clients in 3x-ui.

Run inside the api container so the script uses the same DATABASE_URL and
THREEXUI_API_TOKEN as the production app.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
API_ROOT = ROOT / "api"
for path in (ROOT, API_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

BYTES_IN_GB = 1024 * 1024 * 1024


def parse_client_id(value: str) -> int:
    normalized = value.strip().lower()
    if normalized.startswith("vpn-"):
        normalized = normalized[4:]
    try:
        client_id = int(normalized)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{value!r} is not a client id or vpn-<id>") from exc
    if client_id <= 0:
        raise argparse.ArgumentTypeError("client id must be positive")
    return client_id


def bytes_to_gb(value: Any) -> float:
    try:
        return int(value or 0) / BYTES_IN_GB
    except (TypeError, ValueError):
        return 0.0


def format_gb(value: Any) -> str:
    return f"{bytes_to_gb(value):.3f} GB"


def format_ts_ms(value: Any) -> str:
    try:
        timestamp_ms = int(value or 0)
    except (TypeError, ValueError):
        return "—"
    if timestamp_ms <= 0:
        return "—"
    return datetime.fromtimestamp(timestamp_ms / 1000, timezone.utc).isoformat()


def find_inbound(rows: list[Any], inbound_tag: str | None) -> dict[str, Any] | None:
    try:
        inbound_id = int(inbound_tag or 0)
    except (TypeError, ValueError):
        return None
    for row in rows:
        if isinstance(row, dict) and row.get("id") == inbound_id:
            return row
    return None


async def load_clients(client_ids: list[int], limit: int) -> list[tuple[VPNClient, VPNNode, VPNNodeConfig, User, Subscription]]:
    from sqlalchemy import desc, select

    from app.db.models import Subscription, User, VPNClient, VPNNode, VPNNodeConfig
    from app.db.session import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        stmt = (
            select(VPNClient, VPNNode, VPNNodeConfig, User, Subscription)
            .join(VPNNode, VPNNode.id == VPNClient.node_id)
            .join(
                VPNNodeConfig,
                (VPNNodeConfig.node_id == VPNClient.node_id)
                & (VPNNodeConfig.protocol == VPNClient.protocol),
            )
            .join(User, User.id == VPNClient.user_id)
            .join(Subscription, Subscription.id == VPNClient.subscription_id)
            .where(VPNNodeConfig.protocol == "vless")
        )
        if client_ids:
            stmt = stmt.where(VPNClient.id.in_(client_ids)).order_by(VPNClient.id)
        else:
            stmt = stmt.order_by(desc(VPNClient.id)).limit(limit)
        result = await db.execute(stmt)
        return list(result.all())


async def check_clients(client_ids: list[int], limit: int) -> list[dict[str, Any]]:
    from app.services.threexui import ThreeXUIClient, ThreeXUIClientNotFound, ThreeXUIError

    rows = await load_clients(client_ids, limit)
    output: list[dict[str, Any]] = []
    inbound_cache: dict[int, list[Any]] = {}

    for client, node, config, user, subscription in rows:
        email = f"vpn-{client.id}"
        api_address = config.config.get("api_address")
        inbound_tag = str(config.config.get("inbound_tag") or "")
        record: dict[str, Any] = {
            "client": email,
            "db_client_status": client.status,
            "db_subscription_status": subscription.status,
            "user_id": user.id,
            "telegram_id": user.telegram_id,
            "username": user.username,
            "email": user.email,
            "node": node.name,
            "region": node.region,
            "inbound_tag": inbound_tag,
            "expires_at": client.expires_at.isoformat() if client.expires_at else None,
            "traffic_limit_gb_db": client.traffic_limit_gb,
            "panel_found": False,
        }
        try:
            panel = ThreeXUIClient(address=api_address, timeout=10.0)
            if config.id not in inbound_cache:
                raw_rows = await panel._request("GET", "inbounds/list")
                inbound_cache[config.id] = raw_rows if isinstance(raw_rows, list) else []
            inbound = find_inbound(inbound_cache[config.id], inbound_tag)
            if inbound:
                record.update(
                    {
                        "inbound_upload": format_gb(inbound.get("up")),
                        "inbound_download": format_gb(inbound.get("down")),
                        "inbound_used": format_gb(int(inbound.get("up") or 0) + int(inbound.get("down") or 0)),
                    }
                )
            traffic = await panel.get_client_traffic(email)
            used_bytes = int(traffic.get("up") or 0) + int(traffic.get("down") or 0)
            total_bytes = int(traffic.get("total") or 0)
            record.update(
                {
                    "panel_found": True,
                    "panel_enabled": traffic.get("enable"),
                    "client_upload": format_gb(traffic.get("up")),
                    "client_download": format_gb(traffic.get("down")),
                    "client_used": format_gb(used_bytes),
                    "client_limit": format_gb(total_bytes) if total_bytes else "unlimited/unknown",
                    "client_left": format_gb(max(total_bytes - used_bytes, 0)) if total_bytes else "unlimited/unknown",
                    "panel_expires_at": format_ts_ms(traffic.get("expiryTime")),
                    "last_online": format_ts_ms(traffic.get("lastOnline")),
                }
            )
        except ThreeXUIClientNotFound as exc:
            record["panel_error"] = str(exc)
        except ThreeXUIError as exc:
            record["panel_error"] = str(exc)
        output.append(record)
    return output


def print_human(records: list[dict[str, Any]]) -> None:
    if not records:
        print("No VPN clients found.")
        return
    for record in records:
        print(f"{record['client']} | node={record['node']} region={record.get('region') or '—'} inbound={record.get('inbound_tag') or '—'}")
        print(
            "  db: "
            f"client={record['db_client_status']} subscription={record['db_subscription_status']} "
            f"expires={record.get('expires_at') or '—'} limit={record.get('traffic_limit_gb_db')} GB"
        )
        print(
            "  user: "
            f"id={record['user_id']} tg={record['telegram_id']} "
            f"username={record.get('username') or '—'} email={record.get('email') or '—'}"
        )
        if record.get("inbound_used"):
            print(
                "  inbound: "
                f"up={record['inbound_upload']} down={record['inbound_download']} used={record['inbound_used']}"
            )
        if record.get("panel_found"):
            print(
                "  panel: "
                f"enabled={record.get('panel_enabled')} used={record.get('client_used')} "
                f"left={record.get('client_left')} limit={record.get('client_limit')}"
            )
            print(
                "  activity: "
                f"last_online={record.get('last_online')} panel_expires={record.get('panel_expires_at')}"
            )
        else:
            print(f"  panel: not found/error={record.get('panel_error') or 'unknown'}")
        print()


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read real traffic counters for VPN clients from the configured 3x-ui node."
    )
    parser.add_argument("clients", nargs="*", type=parse_client_id, help="VPN client IDs: 80 or vpn-80")
    parser.add_argument("--limit", type=int, default=10, help="How many latest clients to show when IDs are omitted")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    args = parser.parse_args()

    records = await check_clients(args.clients, max(args.limit, 1))
    if args.json:
        print(json.dumps(records, ensure_ascii=False, indent=2, default=str))
    else:
        print_human(records)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
