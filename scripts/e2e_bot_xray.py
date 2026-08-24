"""Run the bot-side purchase path against a deployed test API.

The script intentionally prints only structural URI fields, not the client UUID
or complete VPN URI. It is meant to run inside the bot container.
"""

import asyncio
import json
import os
from urllib.parse import parse_qs, urlsplit

from app.domain import country_label
from app.main import (
    api_client,
    create_payment,
    get_nodes,
    get_plans,
    get_vpn_client_config,
    get_vpn_status,
)


async def main() -> None:
    telegram_id = int(os.environ["E2E_TELEGRAM_ID"])
    expect_new = os.getenv("E2E_EXPECT_NEW", "true").lower() == "true"
    async with api_client(timeout=15.0) as client:
        response = await client.get(f"/users/{telegram_id}")
        if response.status_code == 404:
            response = await client.post(
                "/users",
                json={
                    "telegram_id": telegram_id,
                    "username": "e2e-lifecycle",
                    "first_name": "E2E",
                    "last_name": "Lifecycle",
                },
            )
        response.raise_for_status()
        user = response.json()

    plans = [plan for plan in await get_plans() if plan["is_active"]]
    nodes = [
        node
        for node in await get_nodes()
        if node["status"] == "active" and country_label(node.get("region"))
    ]
    if not plans or not nodes:
        raise RuntimeError("An active plan and a US/NL/DE node are required")

    plan = plans[0]
    node = nodes[0]
    async with api_client(timeout=15.0) as client:
        before_response = await client.get(f"/vpn/nodes/{node['id']}/health")
        before_response.raise_for_status()
        health_before = before_response.json()

    payment = await create_payment(
        user_id=user["id"],
        plan_id=plan["id"],
        node_id=node["id"],
        client_type="amnezia",
        flow="xtls-rprx-vision",
        idempotency_key=f"e2e:{telegram_id}",
    )
    if payment["status"] != "paid" or not payment.get("subscription_id"):
        raise RuntimeError(f"Mock payment was not confirmed: {payment['status']}")

    status = await get_vpn_status(telegram_id)
    subscription = status.get("subscription")
    vpn_client = status.get("vpn_client")
    if not subscription or not vpn_client:
        raise RuntimeError("Paid payment did not produce an active VPN client")
    if vpn_client["client_type"] != "amnezia":
        raise RuntimeError("The issued client is not marked as AmneziaVPN")

    config = await get_vpn_client_config(vpn_client["id"])
    parsed = urlsplit(config["config"])
    query = parse_qs(parsed.query)
    if parsed.scheme != "vless":
        raise RuntimeError("Expected a VLESS URI")
    if query.get("security") != ["reality"]:
        raise RuntimeError("Expected Reality security")
    if query.get("flow") != ["xtls-rprx-vision"]:
        raise RuntimeError("Expected XTLS Vision flow")

    async with api_client(timeout=15.0) as client:
        after_response = await client.get(f"/vpn/nodes/{node['id']}/health")
        after_response.raise_for_status()
        health_after = after_response.json()

    if health_after.get("status") != "online":
        raise RuntimeError(f"Xray healthcheck failed: {health_after}")
    before_count = health_before.get("xray_users", 0)
    after_count = health_after.get("xray_users", 0)
    if expect_new and after_count < before_count + 1:
        raise RuntimeError("The Xray user count did not increase")
    if not expect_new and after_count != before_count:
        raise RuntimeError("Idempotent replay unexpectedly changed Xray user count")

    print(
        json.dumps(
            {
                "telegram_id": telegram_id,
                "user_id": user["id"],
                "payment_id": payment["id"],
                "subscription_id": subscription["id"],
                "client_id": vpn_client["id"],
                "client_type": vpn_client["client_type"],
                "node_id": node["id"],
                "node_region": node.get("region"),
                "xray_users_before": health_before.get("xray_users"),
                "xray_users_after": health_after.get("xray_users"),
                "expected_new_client": expect_new,
                "uri": {
                    "scheme": parsed.scheme,
                    "host": parsed.hostname,
                    "port": parsed.port,
                    "security": query["security"][0],
                    "flow": query["flow"][0],
                    "fingerprint": query.get("fp", [None])[0],
                },
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
