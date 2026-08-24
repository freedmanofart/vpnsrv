#!/usr/bin/env python3
"""Small, dependency-free manager for the project's .env configuration."""

from __future__ import annotations

import argparse
import os
import secrets
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Variable:
    secret: bool = False
    required: bool = False
    generated_bytes: int | None = None


VARIABLES: dict[str, Variable] = {
    "POSTGRES_DB": Variable(required=True),
    "POSTGRES_USER": Variable(required=True),
    "POSTGRES_PASSWORD": Variable(secret=True, required=True, generated_bytes=24),
    "DATABASE_URL": Variable(secret=True, required=True),
    "REDIS_URL": Variable(required=True),
    "LOG_LEVEL": Variable(),
    "XRAY_API_ADDRESS": Variable(required=True),
    "XRAY_INBOUND_TAG": Variable(required=True),
    "BOT_TOKEN": Variable(secret=True, required=True),
    "TELEGRAM_CHANNEL_URL": Variable(),
    "SUPPORT_URL": Variable(),
    "API_URL": Variable(required=True),
    "SERVICE_API_TOKEN": Variable(secret=True, required=True, generated_bytes=32),
    "PAYMENT_PROVIDER": Variable(required=True),
    "PAYMENT_WEBHOOK_SECRET": Variable(secret=True, required=True, generated_bytes=32),
    "PAYMENT_AUTO_CONFIRM": Variable(required=True),
    "PROMO_CODES": Variable(),
    "ADMIN_USERNAME": Variable(required=True),
    "ADMIN_PASSWORD": Variable(secret=True, required=True, generated_bytes=24),
    "BACKGROUND_JOBS_ENABLED": Variable(required=True),
    "LIFECYCLE_INTERVAL_SECONDS": Variable(),
    "LIFECYCLE_ADVISORY_LOCK_KEY": Variable(),
    "WORKER_RUN_ONCE": Variable(),
    "XRAY_MANAGEMENT_MODE": Variable(required=True),
    "NODE_AGENT_TOKEN": Variable(secret=True),
    "NODE_AGENT_NODE_ID": Variable(),
    "NODE_AGENT_INTERVAL_SECONDS": Variable(),
    "CONTROL_PLANE_URL": Variable(),
    "GRAFANA_ADMIN_USER": Variable(required=True),
    "GRAFANA_ADMIN_PASSWORD": Variable(secret=True, required=True, generated_bytes=24),
}


class EnvFile:
    def __init__(self, path: Path):
        self.path = path
        self.lines = path.read_text().splitlines() if path.exists() else []

    def values(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for line in self.lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            result[key] = value
        return result

    def set(self, key: str, value: str) -> None:
        if "\n" in value or "\r" in value:
            raise ValueError("Multiline values are not supported")
        prefix = f"{key}="
        for index, line in enumerate(self.lines):
            if line.startswith(prefix):
                self.lines[index] = prefix + value
                return
        if self.lines and self.lines[-1] != "":
            self.lines.append("")
        self.lines.append(prefix + value)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.tmp")
        temporary.write_text("\n".join(self.lines).rstrip() + "\n")
        os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(temporary, self.path)


def mask(value: str) -> str:
    if not value:
        return "<empty>"
    if len(value) <= 8:
        return "********"
    return f"{value[:4]}…{value[-4:]}"


def validate(values: dict[str, str]) -> list[str]:
    errors: list[str] = []
    for key, metadata in VARIABLES.items():
        value = values.get(key, "")
        if metadata.required and not value:
            errors.append(f"{key}: missing")
        if metadata.secret and value in {"change_me", "changeme", "secret"}:
            errors.append(f"{key}: placeholder value")
    if values.get("PAYMENT_PROVIDER") != "mock" and values.get(
        "PAYMENT_AUTO_CONFIRM", "false"
    ).lower() == "true":
        errors.append("PAYMENT_AUTO_CONFIRM must be false for a non-mock provider")
    if values.get("XRAY_MANAGEMENT_MODE") not in {"direct", "agent"}:
        errors.append("XRAY_MANAGEMENT_MODE must be direct or agent")
    return errors


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="configctl")
    result.add_argument(
        "--env-file",
        type=Path,
        default=Path(os.getenv("VPN_ENV_FILE", ".env")),
    )
    subparsers = result.add_subparsers(dest="command", required=True)
    list_parser = subparsers.add_parser("list")
    list_parser.add_argument("--show-secrets", action="store_true")
    get_parser = subparsers.add_parser("get")
    get_parser.add_argument("key")
    get_parser.add_argument("--show-secret", action="store_true")
    set_parser = subparsers.add_parser("set")
    set_parser.add_argument("key")
    set_parser.add_argument("value")
    generate_parser = subparsers.add_parser("generate")
    generate_parser.add_argument("key")
    subparsers.add_parser("validate")
    apply_parser = subparsers.add_parser("apply")
    apply_parser.add_argument(
        "--services",
        nargs="+",
        default=["api", "bot", "worker"],
    )
    return result


def main() -> int:
    args = parser().parse_args()
    env = EnvFile(args.env_file)
    values = env.values()

    if args.command == "list":
        for key in sorted(set(VARIABLES) | set(values)):
            value = values.get(key, "")
            metadata = VARIABLES.get(key, Variable())
            rendered = value if args.show_secrets or not metadata.secret else mask(value)
            print(f"{key}={rendered}")
        return 0

    if args.command == "get":
        if args.key not in values:
            raise SystemExit(f"Unknown or unset variable: {args.key}")
        metadata = VARIABLES.get(args.key, Variable())
        print(values[args.key] if args.show_secret or not metadata.secret else mask(values[args.key]))
        return 0

    if args.command == "set":
        env.set(args.key, args.value)
        env.save()
        print(f"updated {args.key}")
        return 0

    if args.command == "generate":
        metadata = VARIABLES.get(args.key)
        if metadata is None or metadata.generated_bytes is None:
            raise SystemExit(f"Generation is not supported for {args.key}")
        env.set(args.key, secrets.token_urlsafe(metadata.generated_bytes))
        env.save()
        print(f"generated {args.key}")
        return 0

    if args.command == "validate":
        errors = validate(values)
        if errors:
            for error in errors:
                print(f"ERROR {error}")
            return 1
        print("configuration is valid")
        return 0


    errors = validate(values)
    if errors:
        raise SystemExit("configuration is invalid; run configctl validate")
    project_directory = args.env_file.resolve().parent
    command = [
        "docker",
        "compose",
        "--project-directory",
        str(project_directory),
        "--env-file",
        str(args.env_file.resolve()),
        "up",
        "-d",
        "--force-recreate",
        *args.services,
    ]
    subprocess.run(command, check=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
