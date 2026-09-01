from __future__ import annotations


PLAN_TIERS = {
    "lite": {
        "label": "🛴 Лайт",
        "connections": 5,
        "traffic": "250 ГБ трафика",
        "summary": "Для одного человека",
    },
    "standard": {
        "label": "🔥 Стандарт",
        "connections": 15,
        "traffic": "650 ГБ трафика",
        "summary": "Идеально для всей семьи",
    },
    "ultra": {
        "label": "🚀 Ультра",
        "connections": 30,
        "traffic": "3 ТБ трафика",
        "summary": "Для стриминга, игр и загрузок",
    },
}


def country_label(region: str | None) -> str | None:
    raw = (region or "").strip()
    if not raw:
        return None
    code, separator, name = raw.partition("|")
    code = code.strip().upper()
    aliases = {"USA": "US", "GERMANY": "DE", "NETHERLANDS": "NL"}
    code = aliases.get(code, code)
    if len(code) != 2 or not code.isalpha():
        return raw
    flag = "".join(chr(127397 + ord(char)) for char in code)
    return f"{flag} {(name if separator and name else code).strip()}"


def subscription_payload(
    user_id: int,
    plan_id: int,
    node_id: int,
) -> dict:
    return {
        "user_id": user_id,
        "plan_id": plan_id,
        "node_id": node_id,
        "client_type": "universal",
        "flow": "",
        "fingerprint": "firefox",
    }


def rotation_payload(node_id: int) -> dict:
    return {
        "node_id": node_id,
        "client_type": "universal",
        "flow": "",
        "fingerprint": "firefox",
    }


def select_public_plans(plans: list[dict], configured_codes: tuple[str, ...]) -> list[dict]:
    """Show every public API plan unless an explicit allowlist is configured."""
    if not configured_codes:
        return plans
    selected = [plan for plan in plans if plan.get("code") in configured_codes]
    order = {code: index for index, code in enumerate(configured_codes)}
    return sorted(selected, key=lambda plan: order.get(plan.get("code"), 999))


def plan_tier(plan: dict) -> str | None:
    code = str(plan.get("code", ""))
    prefix = code.partition("_")[0]
    if prefix in PLAN_TIERS:
        return prefix
    connections = plan.get("max_connections")
    return next(
        (key for key, value in PLAN_TIERS.items() if value["connections"] == connections),
        None,
    )


def plans_by_tier(plans: list[dict]) -> dict[str, list[dict]]:
    result = {key: [] for key in PLAN_TIERS}
    for plan in plans:
        tier = plan_tier(plan)
        if tier:
            result[tier].append(plan)
    return {key: value for key, value in result.items() if value}


def supports_threexui(configs: list[dict]) -> bool:
    """Return whether a logical node can provision VLESS through 3x-ui."""
    for item in configs:
        if item.get("protocol") != "vless":
            continue
        config = item.get("config")
        if not isinstance(config, dict):
            continue
        address = str(config.get("api_address", ""))
        inbound_id = str(config.get("inbound_tag", ""))
        if address.startswith(("http://", "https://")) and inbound_id.isdigit():
            if int(inbound_id) > 0:
                return True
    return False
