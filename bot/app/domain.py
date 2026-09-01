from __future__ import annotations


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
