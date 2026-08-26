"""Проверить activation/profile/debug/refresh устройства без печати секретов."""

import json
import os

import httpx


def main() -> None:
    """Проверить полный жизненный цикл device token через работающий API."""
    telegram_id = int(os.environ["E2E_TELEGRAM_ID"])
    api_url = os.getenv("E2E_API_URL", "http://127.0.0.1:8000")
    service_headers = {
        "Authorization": f"Bearer {os.environ['SERVICE_API_TOKEN']}"
    }
    admin_auth = (
        os.environ["ADMIN_USERNAME"],
        os.environ["ADMIN_PASSWORD"],
    )

    # Сервисная авторизация может выпустить короткоживущий activation code;
    # следующие запросы профиля используют только scoped token устройства.
    with httpx.Client(base_url=api_url, timeout=15.0) as client:
        code_response = client.post(
            "/v1/client/activation-codes",
            headers=service_headers,
            json={"telegram_id": telegram_id, "ttl_minutes": 5},
        )
        code_response.raise_for_status()
        activation = client.post(
            "/v1/client/activate",
            json={
                "code": code_response.json()["code"],
                "name": "E2E device",
                "platform": "test",
            },
        )
        activation.raise_for_status()
        device_id = activation.json()["device_id"]
        old_token = activation.json()["access_token"]
        old_headers = {"Authorization": f"Bearer {old_token}"}

        profile = client.get("/v1/client/profile", headers=old_headers)
        profile.raise_for_status()
        nodes = profile.json()["nodes"]
        if not nodes or not nodes[0]["config"].startswith("vless://"):
            raise RuntimeError("Device profile does not contain VLESS configuration")

        # Sensitive-debug подтверждает, что профиль доступен при активном серверном
        # аудите. При успехе сессия закрывается и не живёт дольше этой проверки.
        debug = client.post(
            "/admin/debug-sessions",
            auth=admin_auth,
            json={"reason": "device E2E", "duration_minutes": 5},
        )
        debug.raise_for_status()
        debug_id = debug.json()["id"]
        debug_profile = client.get("/v1/client/profile", headers=old_headers)
        debug_profile.raise_for_status()

        # Refresh должен быть атомарным: новый токен принимается, а старый
        # отклоняется немедленно, без ожидания фоновой задачи очистки.
        refreshed = client.post("/v1/client/refresh", headers=old_headers)
        refreshed.raise_for_status()
        new_headers = {
            "Authorization": f"Bearer {refreshed.json()['access_token']}"
        }
        old_rejected = client.get("/v1/client/profile", headers=old_headers)
        new_accepted = client.get("/v1/client/profile", headers=new_headers)
        new_accepted.raise_for_status()

        close_debug = client.delete(
            f"/admin/debug-sessions/{debug_id}", auth=admin_auth
        )
        close_debug.raise_for_status()
        revoke = client.delete(f"/admin/devices/{device_id}", auth=admin_auth)
        revoke.raise_for_status()

    # Проверяем важный для безопасности негативный сценарий до вывода компактного
    # JSON без секретов, предназначенного для журналов CI.
    if old_rejected.status_code != 401:
        raise RuntimeError("Old device token remained valid after refresh")
    print(
        json.dumps(
            {
                "device_id": device_id,
                "profile_nodes": len(nodes),
                "protocol": nodes[0]["protocol"],
                "available": nodes[0]["available"],
                "old_token_status": old_rejected.status_code,
                "new_token_status": new_accepted.status_code,
                "debug_session_closed": close_debug.json()["status"],
                "device_status": revoke.json()["status"],
            }
        )
    )


if __name__ == "__main__":
    main()
