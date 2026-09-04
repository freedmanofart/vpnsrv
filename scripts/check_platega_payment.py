#!/usr/bin/env python3
"""Create a Platega test payment and immediately read its status.

The script intentionally prints only non-secret values. It uses the same
PLATEGA_* environment variables that production services receive from compose.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from decimal import Decimal
from typing import Any

import httpx


METHOD_ENV = {
    "sbp": ("PLATEGA_METHOD_SBP_QR", "СБП (QR)"),
    "mir": ("PLATEGA_METHOD_MIR_CARD", "Карта МИР"),
    "crypto": ("PLATEGA_METHOD_CRYPTO", "Криптовалюта"),
}


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise SystemExit(f"{name} is empty")
    return value


def method_id(method: str) -> int | None:
    env_name, _ = METHOD_ENV[method]
    raw = os.getenv(env_name, "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise SystemExit(f"{env_name} must be numeric, got {raw!r}") from exc


def safe_url(value: Any) -> str:
    if not value:
        return "-"
    text = str(value)
    if len(text) <= 140:
        return text
    return text[:137] + "..."


async def create_and_check(method: str, amount: Decimal, currency: str, description: str) -> bool:
    base_url = os.getenv("PLATEGA_BASE_URL", "https://app.platega.io").rstrip("/")
    merchant_id = require_env("PLATEGA_MERCHANT_ID")
    secret = require_env("PLATEGA_SECRET")
    return_url = require_env("PLATEGA_RETURN_URL")
    failed_url = require_env("PLATEGA_FAILED_URL")
    platega_method_id = method_id(method)
    _, label = METHOD_ENV[method]

    if platega_method_id is None:
        print(f"SKIP {method}: paymentMethod is empty ({label})")
        return False

    payload = {
        "paymentMethod": platega_method_id,
        "paymentDetails": {
            "amount": float(amount),
            "currency": currency,
        },
        "description": description,
        "return": return_url,
        "failedUrl": failed_url,
        "payload": f"check=platega_{method}",
        "metadata": {
            "userId": "platega-check",
            "userName": "platega-check",
            "clientIp": "127.0.0.1",
        },
    }
    headers = {
        "X-MerchantId": merchant_id,
        "X-Secret": secret,
        "Content-Type": "application/json",
    }

    print(f"CREATE {method}: {label}, paymentMethod={platega_method_id}, amount={amount} {currency}")
    async with httpx.AsyncClient(base_url=base_url, timeout=30.0) as client:
        create_response = await client.post("/transaction/process", json=payload, headers=headers)
        print(f"CREATE_HTTP_STATUS={create_response.status_code}")
        try:
            create_data = create_response.json()
        except ValueError:
            print(f"CREATE_BODY={create_response.text[:500]}")
            create_response.raise_for_status()
            return False

        if create_response.status_code >= 400:
            print(f"CREATE_ERROR={create_data}")
            return False

        transaction_id = create_data.get("transactionId") or create_data.get("id")
        print(f"TRANSACTION_ID={transaction_id}")
        print(f"PAYMENT_METHOD={create_data.get('paymentMethod')}")
        print(f"STATUS={create_data.get('status')}")
        print(f"REDIRECT={safe_url(create_data.get('redirect'))}")
        print(f"EXPIRES_IN={create_data.get('expiresIn')}")

        if not transaction_id:
            return False

        status_response = await client.get(f"/transaction/{transaction_id}", headers=headers)
        print(f"STATUS_HTTP_STATUS={status_response.status_code}")
        try:
            status_data = status_response.json()
        except ValueError:
            print(f"STATUS_BODY={status_response.text[:500]}")
            status_response.raise_for_status()
            return False

        if status_response.status_code >= 400:
            print(f"STATUS_ERROR={status_data}")
            return False

        print(f"STATUS_CHECK={status_data.get('status')}")
        print(f"STATUS_QR={safe_url(status_data.get('qr'))}")
        print(f"STATUS_PAYFORM_SUCCESS_URL={safe_url(status_data.get('payformSuccessUrl'))}")
        return True


async def amain() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--method", choices=["sbp", "mir", "crypto", "all"], default="sbp")
    parser.add_argument("--amount", type=Decimal, default=Decimal("10"))
    parser.add_argument("--currency", default="RUB")
    parser.add_argument("--description", default="Freedom VPN Platega test")
    args = parser.parse_args()

    methods = ["sbp", "mir", "crypto"] if args.method == "all" else [args.method]
    ok_count = 0
    for index, method in enumerate(methods):
        if index:
            print("")
        if await create_and_check(method, args.amount, args.currency, args.description):
            ok_count += 1

    if ok_count == 0:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(amain())
