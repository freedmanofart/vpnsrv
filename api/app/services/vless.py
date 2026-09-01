from __future__ import annotations

import json
from urllib.parse import urlencode


def build_vless_url(
    *,
    uuid: str,
    host: str,
    port: int,
    config: dict,
    remark: str | None = None,
) -> str:
    params: dict[str, str] = {}

    for key in (
        "encryption",
        "extra",
        "fp",
        "mode",
        "path",
        "pbk",
        "security",
        "sid",
        "sni",
        "spx",
        "type",
        "x_padding_bytes",
        "flow",
        "alpn",
        "serviceName",
    ):
        value = config.get(key)

        if value is not None:
            if key == "extra" and not isinstance(value, str):
                value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            params[key] = str(value)

    if config.get("type") == "xhttp":
        params["host"] = str(config.get("xhttp_host", ""))

    query = urlencode(params)

    fragment = remark or "VPN"

    return (
        f"vless://{uuid}@{host}:{port}"
        f"?{query}"
        f"#{fragment}"
    )
