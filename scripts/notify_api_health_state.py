#!/usr/bin/env python3
"""Fallback Telegram notification when the API-side alert worker cannot run."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


STATE_FILE = Path(
    os.environ.get(
        "FREEDOMVPN_API_ALERT_STATE_FILE",
        "/var/lib/vpn-service/admin-api-alert-state.json",
    )
)


def _container_env() -> dict[str, str]:
    result = subprocess.run(
        ["docker", "inspect", "vpn-api"],
        capture_output=True,
        text=True,
        timeout=8,
        check=True,
    )
    container = json.loads(result.stdout)[0]
    return dict(
        item.split("=", 1)
        for item in container.get("Config", {}).get("Env", [])
        if "=" in item
    )


def _admin_chat_id(database_url: str) -> str:
    parsed = urllib.parse.urlsplit(database_url.replace("postgresql+asyncpg://", "postgresql://"))
    username = urllib.parse.unquote(parsed.username or "")
    database = urllib.parse.unquote(parsed.path.lstrip("/"))
    if not username or not database:
        return ""
    result = subprocess.run(
        [
            "docker",
            "exec",
            "postgres",
            "psql",
            "-U",
            username,
            "-d",
            database,
            "-Atc",
            "SELECT value FROM admin_settings WHERE key='bot_admin_chat_id' LIMIT 1",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _send(token: str, chat_id: str, message: str) -> bool:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = urllib.parse.urlencode({"chat_id": chat_id, "text": message[:4096]}).encode()
    request = urllib.request.Request(url, data=body, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return payload.get("ok") is True
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        return False


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in {"online", "offline"}:
        print("usage: notify_api_health_state.py online|offline", file=sys.stderr)
        return 2
    new_state = sys.argv[1]
    try:
        current = json.loads(STATE_FILE.read_text(encoding="utf-8")).get("state", "online")
    except (OSError, ValueError, TypeError):
        current = "online"
    if new_state == current:
        return 0

    try:
        env = _container_env()
        token = env.get("BOT_TOKEN", "")
        chat_id = _admin_chat_id(env.get("DATABASE_URL", ""))
    except (OSError, subprocess.SubprocessError, IndexError, ValueError, KeyError):
        token, chat_id = "", ""
    if token and chat_id:
        message = (
            "🚨 Freedom VPN: API alert monitor недоступен. Проверка или доступ к БД завершились ошибкой."
            if new_state == "offline"
            else "✅ Freedom VPN: API alert monitor восстановлен."
        )
        if not _send(token, chat_id, message):
            print("fallback_alert_delivery_failed", file=sys.stderr)
            return 1
    else:
        print("fallback_alert_config_unavailable", file=sys.stderr)
        return 1

    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix="api-alert-", suffix=".tmp", dir=STATE_FILE.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as temp_file:
            json.dump({"state": new_state}, temp_file)
            temp_file.write("\n")
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, STATE_FILE)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
