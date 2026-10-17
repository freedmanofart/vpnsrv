#!/usr/bin/env python3
import json
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / ".env"
CONFIG = ROOT / "state" / "openclaw.json"
SERVER_NAME = "freedomvpn-admin"
SUPPORT_GROUP_ID = "-1004331664224"
LEGACY_SUPPORT_GROUP_ID = "-5543652367"
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
SUPPORT_GROUP_READ_TOOLS = [
    "freedomvpn-health__freedomvpn_health",
    "freedomvpn-admin__freedomvpn_capabilities",
    "freedomvpn-admin__freedomvpn_lookup_user",
    "freedomvpn-admin__freedomvpn_lookup_payments",
    "freedomvpn-admin__freedomvpn_get_system_configuration",
    "freedomvpn-admin__freedomvpn_list_plans",
    "freedomvpn-admin__freedomvpn_list_nodes",
    "freedomvpn-admin__freedomvpn_list_pending_actions",
]
SUPPORT_GROUP_WRITE_DENY = [
    "freedomvpn-admin__freedomvpn_prepare_*",
    "freedomvpn-admin__freedomvpn_approve_action",
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


def telegram_group_admin_ids(token: str) -> tuple[list[str], int]:
    request = Request(
        f"https://api.telegram.org/bot{token}/getChatAdministrators",
        data=json.dumps(
            {"chat_id": int(SUPPORT_GROUP_ID), "return_bots": True}
        ).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=15) as response:
            result = json.load(response)
    except Exception:
        raise SystemExit(
            "Could not retrieve Telegram group administrators; config was not changed"
        ) from None
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise SystemExit("Telegram did not return the support group administrator list; config was not changed")

    administrators = [
        item
        for item in result.get("result", [])
        if isinstance(item, dict)
        and item.get("status") in {"creator", "administrator"}
        and isinstance(item.get("user"), dict)
        and re.fullmatch(r"[0-9]{4,15}", str(item["user"].get("id", "")))
    ]
    admin_ids = sorted({str(item["user"]["id"]) for item in administrators})
    if not admin_ids:
        raise SystemExit("No Telegram group administrators were returned; config was not changed")
    bot_admin_count = sum(1 for item in administrators if item["user"].get("is_bot") is True)
    return admin_ids, bot_admin_count


def strip_admin_tool_grants(policy: dict) -> None:
    for field in ("allow", "alsoAllow"):
        grants = policy.get(field)
        if isinstance(grants, list):
            filtered = [
                item for item in grants
                if not isinstance(item, str) or not item.startswith("freedomvpn-admin__")
            ]
            if filtered:
                policy[field] = filtered
            else:
                policy.pop(field, None)


def main() -> None:
    env = read_env()
    owner_id = env.get("OPENCLAW_OWNER_TELEGRAM_ID", "")
    mcp_token = env.get("OPENCLAW_ADMIN_MCP_TOKEN", "")
    if not re.fullmatch(r"[0-9]{4,15}", owner_id):
        raise SystemExit("OPENCLAW_OWNER_TELEGRAM_ID must be a numeric Telegram user ID")
    telegram_token = env.get("TELEGRAM_BOT_TOKEN", "")
    if not telegram_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN must be set to retrieve current group administrators")
    if not re.fullmatch(r"[A-Za-z0-9_-]{40,}", mcp_token):
        raise SystemExit("OPENCLAW_ADMIN_MCP_TOKEN must be set to a separate random token")
    support_admin_ids, bot_admin_count = telegram_group_admin_ids(telegram_token)
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
        legacy_group = groups.pop(LEGACY_SUPPORT_GROUP_ID, None)
        if legacy_group is None:
            raise SystemExit("Freedom VPN support group is missing from Telegram configuration")
        groups[SUPPORT_GROUP_ID] = legacy_group
    else:
        groups.pop(LEGACY_SUPPORT_GROUP_ID, None)
    for group_id, group in groups.items():
        if not isinstance(group, dict):
            continue
        group_policy = group.setdefault("tools", {})
        deny = group_policy.setdefault("deny", [])
        if not isinstance(deny, list):
            raise SystemExit(f"OpenClaw Telegram group tools.deny is invalid for {group_id}")
        if group_id == SUPPORT_GROUP_ID:
            strip_admin_tool_grants(group_policy)
            group_policy.pop("allow", None)
            if "freedomvpn-admin__*" not in deny:
                deny.append("freedomvpn-admin__*")
            for tool in SUPPORT_GROUP_WRITE_DENY:
                if tool not in group_policy["deny"]:
                    group_policy["deny"].append(tool)

            existing_sender_policies = group.get("toolsBySender", {})
            if not isinstance(existing_sender_policies, dict):
                raise SystemExit("OpenClaw support group toolsBySender must be an object")
            sender_policies = {}
            for telegram_id in support_admin_ids:
                sender_policies[f"id:{telegram_id}"] = {
                    "allow": list(SUPPORT_GROUP_READ_TOOLS)
                }
            group["toolsBySender"] = sender_policies
        elif "freedomvpn-admin__*" not in deny:
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
    print(
        "Read-only Freedom VPN tools configured for "
        f"{len(support_admin_ids)} current Telegram group administrators "
        f"({bot_admin_count} bot administrators; owner is admin: "
        f"{'yes' if owner_id in support_admin_ids else 'no'}); original config backed up."
    )


if __name__ == "__main__":
    main()
