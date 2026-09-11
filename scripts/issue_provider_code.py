"""Issue a short-lived mobile provider code without exposing the service token."""

import argparse
import getpass
import json
import os
from urllib.parse import urlsplit

import httpx


def issue_code(client, telegram_id: int, ttl_minutes: int) -> dict:
    response = client.post(
        "/v1/client/activation-codes",
        json={"telegram_id": telegram_id, "ttl_minutes": ttl_minutes},
    )
    if response.status_code != 200:
        raise RuntimeError(f"Code issuance failed: HTTP {response.status_code}")
    data = response.json()
    code = data.get("code", "")
    if len(code) != 8 or not code.isascii() or not code.isdigit():
        raise RuntimeError("API returned an invalid activation code")
    return {"code": code, "expires_at": data["expires_at"]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--telegram-id", required=True, type=int)
    parser.add_argument("--ttl-minutes", type=int, choices=range(1, 61), default=10)
    args = parser.parse_args()

    url = urlsplit(args.api_url)
    if url.username or url.password or url.query or url.fragment or not url.hostname:
        parser.error("Use a base URL without credentials, query or fragment")
    if url.scheme != "https" and not (
        url.scheme == "http" and url.hostname in ("localhost", "127.0.0.1", "::1")
    ):
        parser.error("HTTPS is required outside localhost")

    token = os.getenv("SERVICE_API_TOKEN") or getpass.getpass("Service API token: ")
    try:
        with httpx.Client(
            base_url=args.api_url.rstrip("/") + "/",
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
            follow_redirects=False,
        ) as client:
            result = issue_code(client, args.telegram_id, args.ttl_minutes)
        print(json.dumps(result, ensure_ascii=False))
    except (httpx.HTTPError, RuntimeError, ValueError, KeyError) as error:
        # Never echo response bodies, URLs or bearer credentials.
        parser.exit(1, f"{type(error).__name__}: code could not be issued\n")


if __name__ == "__main__":
    main()
