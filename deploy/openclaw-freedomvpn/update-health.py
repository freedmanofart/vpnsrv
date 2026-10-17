#!/usr/bin/env python3
import json
import os
import re
import tempfile
from pathlib import Path

SOURCE = Path("/var/backups/vpn-service/tailscale-health.json")
TARGET = Path("/opt/openclaw-freedomvpn/health/health.json")
SECRET_VALUE = re.compile(r"(token|password|secret|authorization|api[_-]?key)=([^&\s]+)", re.I)
BOT_TOKEN = re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b")

def safe_text(value, limit=600):
    text = str(value or "")
    text = SECRET_VALUE.sub(r"\1=[redacted]", text)
    text = BOT_TOKEN.sub("[redacted-token]", text)
    return text[:limit]

def main():
    source = json.loads(SOURCE.read_text(encoding="utf-8"))
    checks = source.get("checks", [])
    if not isinstance(checks, list):
        checks = []
    snapshot = {
        "generated_at": safe_text(source.get("generated_at", "unknown"), 80),
        "status": safe_text(source.get("status", "unknown"), 40),
        "details": safe_text(source.get("details", ""), 300),
        "checks": [
            {
                "id": safe_text(item.get("id", ""), 128),
                "name": safe_text(item.get("name", ""), 160),
                "status": safe_text(item.get("status", "unknown"), 40),
                "details": safe_text(item.get("details", ""), 600),
                **({"latency_ms": item["latency_ms"]} if isinstance(item.get("latency_ms"), (int, float)) else {}),
            }
            for item in checks[:100]
            if isinstance(item, dict)
        ],
    }
    TARGET.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix="health-", suffix=".tmp", dir=TARGET.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(snapshot, output, ensure_ascii=False, separators=(",", ":"))
            output.write("\n")
        os.chmod(temp_name, 0o644)
        os.replace(temp_name, TARGET)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass

if __name__ == "__main__":
    main()
