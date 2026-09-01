#!/usr/bin/env python3
"""Идемпотентно регистрирует child в 3x-ui master и в VPN Admin."""

import argparse
import json
import os
import ssl
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


def env(name, default=None, required=False):
    value = os.getenv(name, default)
    if required and not value:
        raise SystemExit(f"Не задана переменная {name}")
    return value


def boolean(value):
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def request(method, url, token, payload=None, verify_tls=True):
    body = json.dumps(payload).encode() if payload is not None else None
    req = Request(url, data=body, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    context = ssl.create_default_context() if verify_tls else ssl._create_unverified_context()
    try:
        with urlopen(req, timeout=15, context=context) as response:
            return json.load(response)
    except HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:500]
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
    except (URLError, TimeoutError) as exc:
        raise RuntimeError(f"Нет связи с {url}: {exc}") from exc


def panel_call(method, master, token, path, payload, verify_tls):
    result = request(method, f"{master.rstrip('/')}/panel/api/{path.lstrip('/')}", token, payload, verify_tls)
    if not result.get("success"):
        raise RuntimeError(result.get("msg") or "3x-ui отклонил запрос")
    return result.get("obj")


def country_from_ip(address):
    url = (
        f"https://ipwho.is/{quote(address, safe='')}"
        "?lang=ru&fields=success,country,country_code,message"
    )
    try:
        with urlopen(url, timeout=8, context=ssl.create_default_context()) as response:
            data = json.load(response)
    except (HTTPError, URLError, TimeoutError, ValueError) as exc:
        raise RuntimeError("Не удалось определить страну публичного IP") from exc
    if not data.get("success") or not data.get("country_code"):
        raise RuntimeError(data.get("message") or "Страна IP не определена")
    return f"{data['country_code'].upper()}|{data.get('country') or data['country_code']}"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--panel-only", action="store_true")
    args = parser.parse_args()

    master = env("THREEXUI_MASTER_URL", required=True)
    admin_token = env("THREEXUI_ADMIN_TOKEN", required=True)
    child_token = env("THREEXUI_CHILD_API_TOKEN", required=True)
    verify = boolean(env("THREEXUI_MASTER_VERIFY_TLS", "true"))
    name = env("THREEXUI_CHILD_NAME", required=True)
    node = {
        "name": name,
        "remark": env("THREEXUI_CHILD_REMARK", "Управляется VPN Service"),
        "scheme": env("THREEXUI_CHILD_SCHEME", "https"),
        "address": env("THREEXUI_CHILD_ADDRESS", required=True),
        "port": int(env("THREEXUI_CHILD_PORT", "443")),
        "basePath": "/" + env("THREEXUI_CHILD_BASE_PATH", required=True).strip("/") + "/",
        "apiToken": child_token,
        "enable": True,
        "allowPrivateAddress": boolean(env("THREEXUI_CHILD_ALLOW_PRIVATE", "false")),
        "tlsVerifyMode": env("THREEXUI_CHILD_TLS_VERIFY_MODE", "system"),
        "pinnedCertSha256": env("THREEXUI_CHILD_PINNED_CERT_SHA256", ""),
        "inboundSyncMode": env("THREEXUI_CHILD_INBOUND_SYNC_MODE", "all"),
        "inboundTags": [x.strip() for x in env("THREEXUI_CHILD_INBOUND_TAGS", "").split(",") if x.strip()],
        "outboundTag": env("THREEXUI_CHILD_OUTBOUND_TAG", "direct"),
    }
    existing = panel_call("GET", master, admin_token, "nodes/list", None, verify)
    found = next((item for item in existing or [] if item.get("name") == name), None)
    action = f"update/{found['id']}" if found else "add"
    method = "POST"
    print(f"3x-ui: {'обновление' if found else 'добавление'} ноды {name}")
    if not args.dry_run:
        panel_call(method, master, admin_token, f"nodes/{action}", node, verify)
        current = panel_call("GET", master, admin_token, "nodes/list", None, verify)
        found = next(item for item in current if item.get("name") == name)
        panel_call("POST", master, admin_token, f"nodes/probe/{found['id']}", None, verify)

    if args.panel_only:
        return
    api_url = env("VPN_API_URL", "http://127.0.0.1:8000").rstrip("/")
    service_token = env("SERVICE_API_TOKEN", required=True)
    public_host = env("VPN_PUBLIC_HOST", env("THREEXUI_CHILD_ADDRESS"))
    public_ip = env("VPN_NODE_IP", env("THREEXUI_CHILD_ADDRESS"))
    region = env("VPN_NODE_REGION") or country_from_ip(public_ip)
    app_node = {
        "name": name,
        "provider": env("VPN_NODE_PROVIDER", "3x-ui"),
        "region": region,
        "hostname": public_host,
        "ip_address": public_ip,
        "capacity": int(env("VPN_NODE_CAPACITY", "100")),
    }
    nodes = request("GET", f"{api_url}/vpn/nodes", service_token)
    logical = next((item for item in nodes if item.get("name") == name), None)
    print(f"VPN Admin: {'обновление' if logical else 'добавление'} логической ноды {name}")
    if args.dry_run:
        return
    if logical:
        logical = request("PATCH", f"{api_url}/vpn/nodes/{logical['id']}", service_token, app_node)
    else:
        logical = request("POST", f"{api_url}/vpn/nodes", service_token, app_node)
    config = {
        "api_address": master,
        "inbound_tag": str(int(env("THREEXUI_INBOUND_ID", required=True))),
        "host": env("VPN_PUBLIC_HOST", required=True),
        "port": int(env("VPN_PUBLIC_PORT", required=True)),
        "type": env("VPN_TRANSPORT", "xhttp"),
        "encryption": env("VPN_VLESS_ENCRYPTION", required=True),
        "security": "reality",
        "sni": env("VPN_REALITY_SNI", required=True),
        "fp": env("VPN_FINGERPRINT", "firefox"),
        "pbk": env("VPN_REALITY_PUBLIC_KEY", required=True),
        "sid": env("VPN_REALITY_SHORT_ID", required=True),
        "spx": env("VPN_REALITY_SPIDER_X", required=True),
        "path": env("VPN_XHTTP_PATH", "/"),
        "mode": env("VPN_XHTTP_MODE", "auto"),
        "x_padding_bytes": env("VPN_XHTTP_PADDING_BYTES", "100-1000"),
    }
    xhttp_host = env("VPN_XHTTP_HOST", "")
    config["xhttp_host"] = xhttp_host
    config["extra"] = {
        "mode": config["mode"],
        "xPaddingBytes": config["x_padding_bytes"],
    }
    request("POST", f"{api_url}/vpn/nodes/{logical['id']}/configs", service_token, {"protocol": "vless", "config": config})
    health = request("GET", f"{api_url}/vpn/nodes/{logical['id']}/health", service_token)
    print(f"Готово: logical_node_id={logical['id']}, health={health.get('status')}")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        raise SystemExit(1)
