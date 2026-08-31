#!/usr/bin/env python3
"""Создать явный sensitive-debug снимок, не печатая содержащиеся в нём значения."""

from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path
from urllib.request import Request, urlopen


def load_env(path: Path) -> dict[str, str]:
    """Прочитать простые записи KEY=VALUE без интерпретации синтаксиса shell.

    Проект записывает этот формат через configctl. Значения намеренно остаются
    без изменений: снятие кавычек или подстановка переменных изменили бы секрет.
    """
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key] = value
    return values


def find_private_keys(value):
    """Рекурсивно собрать известные поля закрытых ключей из JSON Xray."""
    found = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key.lower() in {"privatekey", "private_key", "reality_private_key"}:
                found.append(item)
            else:
                found.extend(find_private_keys(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(find_private_keys(item))
    return found


def request_json(url: str, auth: str, *, method: str = "GET", body=None):
    """Выполнить один авторизованный JSON-запрос с ограниченным тайм-аутом."""
    headers = {"Authorization": auth}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    request = Request(url, data=data, headers=headers, method=method)
    with urlopen(request, timeout=15) as response:
        return json.load(response)


def main() -> None:
    """Собрать секреты, разрешённые активной серверной debug-сессией."""
    parser = argparse.ArgumentParser()
    parser.add_argument("session_id", type=int)
    parser.add_argument("--project", type=Path, default=Path("."))
    parser.add_argument("--api-url", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    env = load_env(args.project / ".env")
    basic_value = base64.b64encode(
        f"{env['ADMIN_USERNAME']}:{env['ADMIN_PASSWORD']}".encode()
    ).decode()
    basic = f"Basic {basic_value}"
    bearer = f"Bearer {env['SERVICE_API_TOKEN']}"

    # Overview содержит ID клиентов, но не полные VPN URI. Каждую активную
    # конфигурацию явно получаем через аудитируемый административный endpoint.
    overview = request_json(f"{args.api_url}/admin/overview", basic)
    client_keys = []
    for client in overview.get("clients", []):
        if client.get("status") != "active":
            continue
        response = request_json(
            f"{args.api_url}/vpn/clients/{client['id']}/config", basic
        )
        client_keys.append(response.get("config"))

    # Allow-list задан явно, чтобы новые переменные окружения не попадали
    # незаметно в чувствительный снимок без проверки кода.
    secret_names = {
        "BOT_TOKEN",
        "POSTGRES_PASSWORD",
        "ADMIN_PASSWORD",
        "SERVICE_API_TOKEN",
        "PAYMENT_WEBHOOK_SECRET",
        "DATABASE_URL",
        "THREEXUI_API_TOKEN",
        "GRAFANA_ADMIN_PASSWORD",
    }
    snapshot = {
        "telegram_token": env.get("BOT_TOKEN"),
        "passwords_and_tokens": {
            key: env.get(key) for key in sorted(secret_names) if env.get(key)
        },
        "authorization_headers": [basic, bearer],
        # Reality private keys are owned by 3x-ui and are intentionally not
        # copied into this control plane's debug snapshot.
        "reality_private_keys": [],
        "vpn_uris": client_keys,
        "client_key_contents": client_keys,
    }
    # За сохранение и аудит отвечает API. Утилита не пишет открытый снимок
    # локально, а её stdout содержит только количества и идентификаторы.
    result = request_json(
        f"{args.api_url}/admin/debug-sessions/{args.session_id}/snapshot",
        basic,
        method="POST",
        body={"secrets": snapshot},
    )
    print(
        json.dumps(
            {
                "captured": bool(result.get("sensitive")),
                "audit_log_id": result.get("audit_log_id"),
                "vpn_keys": len(client_keys),
                "reality_private_keys": len(snapshot["reality_private_keys"]),
                "secret_variables": len(snapshot["passwords_and_tokens"]),
            }
        )
    )


if __name__ == "__main__":
    main()
