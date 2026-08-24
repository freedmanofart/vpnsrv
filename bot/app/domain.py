from __future__ import annotations


COUNTRY_LABELS = {
    "us": "🇺🇸 США",
    "usa": "🇺🇸 США",
    "united states": "🇺🇸 США",
    "сша": "🇺🇸 США",
    "nl": "🇳🇱 Нидерланды",
    "netherlands": "🇳🇱 Нидерланды",
    "нидерланды": "🇳🇱 Нидерланды",
    "de": "🇩🇪 Германия",
    "germany": "🇩🇪 Германия",
    "германия": "🇩🇪 Германия",
}


def country_label(region: str | None) -> str | None:
    return COUNTRY_LABELS.get((region or "").strip().lower())


def profile_flow(profile: str) -> str:
    if profile == "standard":
        return ""
    if profile == "vision":
        return "xtls-rprx-vision"
    raise ValueError("Unsupported VLESS profile")


def subscription_payload(
    user_id: int,
    plan_id: int,
    node_id: int,
    client_type: str,
    profile: str,
) -> dict:
    if client_type not in {"amnezia", "universal"}:
        raise ValueError("Unsupported client type")
    return {
        "user_id": user_id,
        "plan_id": plan_id,
        "node_id": node_id,
        "client_type": client_type,
        "flow": profile_flow(profile),
        "fingerprint": "chrome",
    }


def rotation_payload(node_id: int, client_type: str, profile: str) -> dict:
    if client_type not in {"amnezia", "universal"}:
        raise ValueError("Unsupported client type")
    return {
        "node_id": node_id,
        "client_type": client_type,
        "flow": profile_flow(profile),
        "fingerprint": "chrome",
    }
