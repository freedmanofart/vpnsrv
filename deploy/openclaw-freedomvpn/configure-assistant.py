#!/usr/bin/env python3
import json
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / ".env"
CONFIG = ROOT / "state" / "openclaw.json"
SERVER_NAME = "freedomvpn-admin"
SUPPORT_GROUP_ID = "-5543652367"
TOOLS = [
    "freedomvpn_capabilities",
    "freedomvpn_lookup_user",
    "freedomvpn_lookup_payments",
    "freedomvpn_get_system_configuration",
    "freedomvpn_list_plans",
    "freedomvpn_list_nodes",
    "freedomvpn_list_pending_actions",
    "freedomvpn_prepare_payment_status",
    "freedomvpn_prepare_platega_refund",
    "freedomvpn_prepare_plan_update",
    "freedomvpn_prepare_node_update",
    "freedomvpn_approve_action",
]


def read_env() -> dict[str, str]:
    values = {}
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("\"'")
    return values


def main() -> None:
    env = read_env()
    owner_id = env.get("OPENCLAW_OWNER_TELEGRAM_ID", "")
    mcp_token = env.get("OPENCLAW_ADMIN_MCP_TOKEN", "")
    if not re.fullmatch(r"[0-9]{4,15}", owner_id):
        raise SystemExit("OPENCLAW_OWNER_TELEGRAM_ID must be a numeric Telegram user ID")
    if not re.fullmatch(r"[A-Za-z0-9_-]{40,}", mcp_token):
        raise SystemExit("OPENCLAW_ADMIN_MCP_TOKEN must be set to a separate random token")
    config = json.loads(CONFIG.read_text(encoding="utf-8"))

    tools = config.setdefault("tools", {})
    global_also = tools.setdefault("alsoAllow", [])
    if not isinstance(global_also, list):
        raise SystemExit("OpenClaw tools.alsoAllow must be a list")
    if "freedomvpn-admin__*" not in global_also:
        global_also.append("freedomvpn-admin__*")

    telegram = config.setdefault("channels", {}).setdefault("telegram", {})
    telegram["dmPolicy"] = "allowlist"
    telegram["allowFrom"] = [owner_id]
    direct = telegram.setdefault("direct", {})
    owner_direct = direct.setdefault(owner_id, {})
    owner_policy = owner_direct.setdefault("tools", {})
    owner_also = owner_policy.setdefault("alsoAllow", [])
    if not isinstance(owner_also, list):
        raise SystemExit("OpenClaw direct tools.alsoAllow must be a list")
    if "freedomvpn-admin__*" not in owner_also:
        owner_also.append("freedomvpn-admin__*")
    commands = config.setdefault("commands", {})
    commands["ownerAllowFrom"] = [f"telegram:{owner_id}"]

    groups = telegram.setdefault("groups", {})
    if SUPPORT_GROUP_ID not in groups:
        raise SystemExit("Freedom VPN support group is missing from Telegram configuration")
    for group_id, group in groups.items():
        if not isinstance(group, dict):
            continue
        group_policy = group.setdefault("tools", {})
        deny = group_policy.setdefault("deny", [])
        if not isinstance(deny, list):
            raise SystemExit(f"OpenClaw Telegram group tools.deny is invalid for {group_id}")
        if "freedomvpn-admin__*" not in deny:
            deny.append("freedomvpn-admin__*")

    servers = config.setdefault("mcp", {}).setdefault("servers", {})
    servers[SERVER_NAME] = {
        "url": "http://openclaw-assistant-mcp:8080/mcp",
        "transport": "streamable-http",
        "enabled": True,
        "connectionTimeoutMs": 5000,
        "requestTimeoutMs": 30000,
        "headers": {"Authorization": f"Bearer {mcp_token}"},
        "toolFilter": {"include": TOOLS},
    }

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = CONFIG.with_name(f"openclaw.json.pre-assistant-{stamp}")
    state_owner = CONFIG.parent.stat()
    shutil.copy2(CONFIG, backup)
    os.chown(backup, state_owner.st_uid, state_owner.st_gid)
    os.chmod(backup, 0o600)
    fd, temp_name = tempfile.mkstemp(prefix="openclaw-", suffix=".json", dir=CONFIG.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(config, output, ensure_ascii=False, indent=2)
            output.write("\n")
        os.chown(temp_name, state_owner.st_uid, state_owner.st_gid)
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, CONFIG)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
    print("Owner-only Freedom VPN assistant tools configured; original config backed up.")


if __name__ == "__main__":
    main()
