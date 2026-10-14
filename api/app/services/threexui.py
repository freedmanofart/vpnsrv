import base64
import json
import secrets
from types import SimpleNamespace
from urllib.parse import quote

import httpx

from app.core.config import settings


class ThreeXUIError(Exception):
    pass


class ThreeXUIClientNotFound(ThreeXUIError):
    pass


class ThreeXUIClientAlreadyExists(ThreeXUIError):
    pass


class ThreeXUIClient:
    """Async client for the API bundled with the project's 3x-ui 3.7 master."""

    def __init__(self, address: str | None = None, timeout: float = 5.0):
        if not address or not address.startswith(("http://", "https://")):
            raise ThreeXUIError("3x-ui master URL must start with http:// or https://")
        self.address = address.rstrip("/")
        self.timeout = timeout

    async def _request(self, method: str, path: str, **kwargs):
        if not settings.threexui_api_token:
            raise ThreeXUIError("3x-ui API token is not configured")
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout,
                verify=settings.threexui_verify_tls,
            ) as client:
                response = await client.request(
                    method,
                    f"{self.address}/panel/api/{path.lstrip('/')}",
                    headers={"Authorization": f"Bearer {settings.threexui_api_token}"},
                    **kwargs,
                )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ThreeXUIError(f"3x-ui request failed: {exc}") from exc
        if not payload.get("success", False):
            raise ThreeXUIError(payload.get("msg") or "3x-ui rejected the request")
        return payload.get("obj")

    @staticmethod
    def _inbound_id(value: str) -> int:
        try:
            inbound_id = int(value)
        except (TypeError, ValueError) as exc:
            raise ThreeXUIError("3x-ui inbound_tag must contain a numeric inbound ID") from exc
        if inbound_id <= 0:
            raise ThreeXUIError("3x-ui inbound ID must be positive")
        return inbound_id

    async def add_vless_user(
        self,
        inbound_tag: str,
        client_uuid: str,
        email: str,
        level: int = 0,
        flow: str = "",
        expiry_time: int = 0,
        telegram_id: int = 0,
        limit_ip: int = 0,
        limit_hwid: int = 0,
        total_gb: int = 0,
    ) -> None:
        del level
        try:
            await self._request(
                "POST",
                "clients/add",
                json={
                    "client": {
                        "email": email,
                        "id": client_uuid,
                        # A 16-byte base64 key is accepted by the shared
                        # password-based inbounds and is required by
                        # Shadowsocks 2022 AES-128 when the client is attached
                        # to the KIT profile set later in provisioning.
                        "password": base64.b64encode(secrets.token_bytes(16)).decode("ascii"),
                        "flow": flow,
                        "enable": True,
                        "totalGB": total_gb,
                        "expiryTime": expiry_time,
                        "tgId": telegram_id,
                        "limitIp": limit_ip,
                        "limitHwid": limit_hwid,
                    },
                    "inboundIds": [self._inbound_id(inbound_tag)],
                },
            )
        except ThreeXUIError as exc:
            if "already" in str(exc).lower() or "exist" in str(exc).lower():
                raise ThreeXUIClientAlreadyExists(f"3x-ui client {email} already exists") from exc
            raise

    async def remove_vless_user(self, inbound_tag: str, email: str) -> None:
        self._inbound_id(inbound_tag)
        try:
            await self._request(
                "POST",
                f"clients/del/{quote(email, safe='')}?keepTraffic=1",
            )
        except ThreeXUIError as exc:
            if "not found" in str(exc).lower() or "does not exist" in str(exc).lower():
                raise ThreeXUIClientNotFound(f"3x-ui client {email} is already absent") from exc
            raise

    async def attach_client(self, email: str, inbound_ids: list[int]) -> None:
        """Attach an existing client to additional explicitly configured inbounds."""

        requested = sorted({self._inbound_id(str(value)) for value in inbound_ids})
        if not requested:
            return
        current = await self.get_client_subscription_info(email)
        missing = [value for value in requested if value not in current["inbound_ids"]]
        if missing:
            await self._request(
                "POST",
                f"clients/{quote(email, safe='')}/attach",
                json={"inboundIds": missing},
            )

    async def update_vless_user(
        self,
        inbound_tag: str,
        client_uuid: str,
        email: str,
        level: int = 0,
        flow: str = "",
        expiry_time: int = 0,
        telegram_id: int = 0,
        limit_ip: int = 0,
        limit_hwid: int = 0,
        total_gb: int = 0,
    ) -> None:
        """Replace a managed VLESS client while preserving its UUID/email."""
        del inbound_tag, level
        try:
            await self._request(
                "POST",
                f"clients/update/{quote(email, safe='')}",
                json={
                    "id": client_uuid,
                    "email": email,
                    "flow": flow,
                    "enable": True,
                    "totalGB": total_gb,
                    "expiryTime": expiry_time,
                    "tgId": telegram_id,
                    "limitIp": limit_ip,
                    "limitHwid": limit_hwid,
                },
            )
        except ThreeXUIError as exc:
            if "not found" in str(exc).lower() or "does not exist" in str(exc).lower():
                raise ThreeXUIClientNotFound(f"3x-ui client {email} is absent") from exc
            raise

    async def get_users(self, inbound_tag: str):
        inbound_id = self._inbound_id(inbound_tag)
        rows = await self._request("GET", "inbounds/list")
        if not isinstance(rows, list):
            raise ThreeXUIError("3x-ui returned an invalid inbound list")
        inbound = next(
            (row for row in rows if isinstance(row, dict) and row.get("id") == inbound_id),
            None,
        )
        if inbound is None:
            raise ThreeXUIError(f"3x-ui inbound {inbound_id} was not found")
        raw_settings = inbound.get("settings") or {}
        if isinstance(raw_settings, str):
            try:
                raw_settings = json.loads(raw_settings)
            except ValueError as exc:
                raise ThreeXUIError("3x-ui returned invalid inbound settings") from exc
        clients = raw_settings.get("clients", []) if isinstance(raw_settings, dict) else []
        return [
            SimpleNamespace(
                email=item.get("email", ""),
                id=item.get("id", ""),
                flow=item.get("flow", ""),
            )
            for item in clients
            if isinstance(item, dict)
        ]

    async def get_client_subscription_info(self, email: str) -> dict:
        """Return the panel subId and inbound IDs for one client without exposing its key."""
        rows = await self._request("GET", "inbounds/list")
        if not isinstance(rows, list):
            raise ThreeXUIError("3x-ui returned an invalid inbound list")

        sub_ids: set[str] = set()
        inbound_ids: set[int] = set()
        for inbound in rows:
            if not isinstance(inbound, dict):
                continue
            raw_settings = inbound.get("settings") or {}
            if isinstance(raw_settings, str):
                try:
                    raw_settings = json.loads(raw_settings)
                except ValueError as exc:
                    raise ThreeXUIError("3x-ui returned invalid inbound settings") from exc
            clients = raw_settings.get("clients", []) if isinstance(raw_settings, dict) else []
            for item in clients:
                if not isinstance(item, dict) or item.get("email") != email:
                    continue
                try:
                    inbound_ids.add(int(inbound["id"]))
                except (KeyError, TypeError, ValueError):
                    continue
                if item.get("subId"):
                    sub_ids.add(str(item["subId"]))

        if len(sub_ids) > 1:
            raise ThreeXUIError("3x-ui returned conflicting subscription IDs for a client")
        return {
            "sub_id": next(iter(sub_ids), ""),
            "inbound_ids": sorted(inbound_ids),
        }

    async def get_client_traffic(self, email: str) -> dict:
        rows = await self._request("GET", "inbounds/list")
        if not isinstance(rows, list):
            raise ThreeXUIError("3x-ui returned invalid inbound traffic")
        for inbound in rows:
            if not isinstance(inbound, dict):
                continue
            stats = inbound.get("clientStats") or []
            for traffic in stats:
                if isinstance(traffic, dict) and traffic.get("email") == email:
                    return traffic
        raise ThreeXUIClientNotFound(f"3x-ui traffic for {email} was not found")
