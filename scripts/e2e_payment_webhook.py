"""Verify signed and idempotent payment webhook behavior on a test payment."""

import hashlib
import hmac
import json
import os

import httpx


def main() -> None:
    payment_id = int(os.environ["E2E_PAYMENT_ID"])
    api_url = os.getenv("E2E_API_URL", "http://127.0.0.1:8000")
    token = os.environ["SERVICE_API_TOKEN"]
    secret = os.environ["PAYMENT_WEBHOOK_SECRET"]
    auth = {"Authorization": f"Bearer {token}"}

    with httpx.Client(base_url=api_url, timeout=15.0) as client:
        payment_response = client.get(f"/payments/{payment_id}", headers=auth)
        payment_response.raise_for_status()
        payment = payment_response.json()
        body = json.dumps(
            {
                "provider_payment_id": payment["provider_payment_id"],
                "status": "refunded",
                "details": {"source": "e2e-webhook"},
            },
            separators=(",", ":"),
        ).encode()
        signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        headers = {
            "Content-Type": "application/json",
            "X-Payment-Event-Id": f"e2e-refund-{payment_id}",
            "X-Payment-Signature": signature,
        }
        first = client.post(
            f"/payments/webhooks/{payment['provider']}",
            content=body,
            headers=headers,
        )
        first.raise_for_status()
        second = client.post(
            f"/payments/webhooks/{payment['provider']}",
            content=body,
            headers=headers,
        )
        second.raise_for_status()

        invalid_headers = {
            **headers,
            "X-Payment-Event-Id": f"e2e-invalid-{payment_id}",
            "X-Payment-Signature": "invalid",
        }
        invalid = client.post(
            f"/payments/webhooks/{payment['provider']}",
            content=body,
            headers=invalid_headers,
        )

    if first.json()["status"] != "refunded" or second.json()["status"] != "refunded":
        raise RuntimeError("Refund transition was not persisted")
    if invalid.status_code != 401:
        raise RuntimeError("Invalid webhook signature was accepted")
    print(
        json.dumps(
            {
                "payment_id": payment_id,
                "first_status": first.status_code,
                "duplicate_status": second.status_code,
                "payment_status": second.json()["status"],
                "invalid_signature_status": invalid.status_code,
            }
        )
    )


if __name__ == "__main__":
    main()
