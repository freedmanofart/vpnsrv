from __future__ import annotations


PLAN_TIERS = {
    "lite": {
        "label": "Лайт",
        "connections": 5,
        "traffic": "250 ГБ трафика",
        "summary": "Для одного человека",
    },
    "standard": {
        "label": "Стандарт",
        "connections": 15,
        "traffic": "650 ГБ трафика",
        "summary": "Идеально для всей семьи",
    },
    "ultra": {
        "label": "Ультра",
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
    package = plan.get("package")
    if isinstance(package, dict) and package.get("code"):
        return str(package["code"])
    package_code = plan.get("package_code")
    if package_code:
        return str(package_code)
    package_id = plan.get("package_id")
    if package_id:
        return f"package_{package_id}"
    code = str(plan.get("code", ""))
    prefix = code.partition("_")[0]
    if prefix in PLAN_TIERS:
        return prefix
    connections = plan.get("max_connections")
    return next(
        (key for key, value in PLAN_TIERS.items() if value["connections"] == connections),
        None,
    )


def package_details(tier: str, packages: list[dict] | None = None) -> dict:
    packages = packages or []
    for package in packages:
        if str(package.get("code")) == tier or f"package_{package.get('id')}" == tier:
            connections = int(package.get("max_connections") or 0)
            traffic_gb = int(package.get("traffic_limit_gb") or 0)
            if traffic_gb >= 1024 and traffic_gb % 1024 == 0:
                traffic = f"{traffic_gb // 1024} ТБ трафика"
            elif traffic_gb:
                traffic = f"{traffic_gb} ГБ трафика"
            else:
                traffic = "без ограничений по трафику"
            summary = package.get("description") or ""
            return {
                "label": package.get("name") or tier,
                "connections": connections,
                "traffic": traffic,
                "summary": summary,
            }
    return PLAN_TIERS.get(
        tier,
        {
            "label": tier,
            "connections": 0,
            "traffic": "трафик по тарифу",
            "summary": "",
        },
    )


def package_line(tier: str, packages: list[dict] | None = None) -> str:
    details = package_details(tier, packages)
    connections = int(details.get("connections") or 0)
    connection_text = "без ограничений" if not connections else f"до {connections} подключений"
    return f"<b>{details['label']}</b> — {connection_text}, {details['traffic']}"


def plans_by_tier(plans: list[dict], packages: list[dict] | None = None) -> dict[str, list[dict]]:
    result = {str(package["code"]): [] for package in packages or [] if package.get("code")}
    if not result:
        result = {key: [] for key in PLAN_TIERS}
    packages_by_id = {
        int(package["id"]): str(package["code"])
        for package in packages or []
        if package.get("id") is not None and package.get("code")
    }
    for plan in plans:
        package_id = plan.get("package_id")
        tier = packages_by_id.get(int(package_id)) if package_id else None
        if tier is None:
            tier = plan_tier(plan)
        if tier:
            result.setdefault(tier, [])
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
