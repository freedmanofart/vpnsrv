"""Deliver an explicit assistant, commit, or test alert to the admin group."""

from __future__ import annotations

import asyncio
import json
import sys

from app.db.session import AsyncSessionLocal
from app.services.notifications import send_admin_telegram_message


def _message(event: dict) -> str:
    kind = event.get("type")
    if kind == "commit":
        return (
            "🧩 Git commit Freedom VPN\n"
            f"Ветка: {str(event.get('branch') or 'unknown')[:160]}\n"
            f"Коммит: {str(event.get('sha') or 'unknown')[:64]}\n"
            f"Автор: {str(event.get('author') or 'unknown')[:160]}\n"
            f"Сообщение: {str(event.get('subject') or '(без сообщения)')[:500]}"
        )
    if kind == "assistant_message":
        return "🤖 Codex → Freedom VPN Admin\n\n" + str(event.get("message") or "")
    if kind in {"test_health", "test_audit", "test_commit", "test_assistant"}:
        label = {
            "test_health": "health alert",
            "test_audit": "audit event alert",
            "test_commit": "commit notification transport",
            "test_assistant": "assistant message delivery",
        }[kind]
        return f"🧪 ТЕСТ: {label}\n\n{str(event.get('message') or 'Доставка в группу настроена.')[:3500]}"
    raise ValueError(f"unsupported admin alert event type: {kind!r}")


async def main() -> int:
    try:
        event = json.load(sys.stdin)
        if not isinstance(event, dict):
            raise ValueError("event must be a JSON object")
        text = _message(event)
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"invalid admin alert event: {exc}", file=sys.stderr)
        return 2

    async with AsyncSessionLocal() as db:
        delivered = await send_admin_telegram_message(db, text)
    if not delivered:
        return 1
    print("delivered")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
