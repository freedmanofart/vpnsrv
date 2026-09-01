import ipaddress

import httpx


async def country_from_ip(address: str) -> str:
    ip = ipaddress.ip_address(address)
    if not ip.is_global:
        raise ValueError("Для определения страны нужен публичный IP")
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            response = await client.get(
                f"https://ipwho.is/{ip}",
                params={"lang": "ru", "fields": "success,country,country_code,message"},
            )
        response.raise_for_status()
        data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise ValueError("Не удалось определить страну по IP") from exc
    if not data.get("success") or not data.get("country_code"):
        raise ValueError(data.get("message") or "Страна IP не определена")
    return f"{data['country_code'].upper()}|{data.get('country') or data['country_code']}"
