import json
import logging
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any


request_id_context: ContextVar[str | None] = ContextVar("request_id", default=None)

SENSITIVE_KEYS = {
    "authorization",
    "bot_token",
    "client_uuid",
    "config",
    "password",
    "private_key",
    "reality_private_key",
    "service_api_token",
    "token",
    "vpn_uri",
}


def redact(value: Any, *, key: str | None = None) -> Any:
    if key and key.lower() in SENSITIVE_KEYS:
        return "<redacted>"
    if isinstance(value, dict):
        return {item_key: redact(item, key=item_key) for item_key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    return value


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        request_id = request_id_context.get()
        if request_id:
            payload["request_id"] = request_id
        event = getattr(record, "event", None)
        if isinstance(event, dict):
            payload.update(redact(event))
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JSONFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())
