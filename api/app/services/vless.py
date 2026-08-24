from urllib.parse import urlencode


def build_vless_url(
    *,
    uuid: str,
    host: str,
    port: int,
    config: dict,
    remark: str | None = None,
) -> str:
    params = {}

    for key in (
        "type",
        "security",
        "encryption",
        "flow",
        "sni",
        "fp",
        "pbk",
        "sid",
        "spx",
    ):
        value = config.get(key)

        if value is not None:
            params[key] = value

    query = urlencode(params)

    fragment = remark or "VPN"

    return (
        f"vless://{uuid}@{host}:{port}"
        f"?{query}"
        f"#{fragment}"
    )
