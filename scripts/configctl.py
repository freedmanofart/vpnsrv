#!/usr/bin/env python3
"""Небольшой менеджер конфигурации .env проекта без внешних зависимостей."""

from __future__ import annotations

import argparse
import os
import secrets
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Variable:
    """Метаданные политики для маскирования, проверки и генерации."""
    secret: bool = False
    required: bool = False
    generated_bytes: int | None = None
    rotation_services: tuple[str, ...] = ()
    requires_external_sync: bool = False


# Центральный allow-list. Неизвестные ключи .env можно менять и просматривать,
# но только объявленные здесь ключи получают специальную проверку и маскирование.
VARIABLES: dict[str, Variable] = {
    "POSTGRES_CONTAINER": Variable(),
    "VPN_DATABASE_NAME": Variable(),
    "DATABASE_URL": Variable(secret=True, required=True),
    "LOG_LEVEL": Variable(),
    "BOT_TOKEN": Variable(secret=True, required=True),
    "TELEGRAM_CHANNEL_URL": Variable(),
    "SUPPORT_URL": Variable(),
    "API_URL": Variable(required=True),
    "SERVICE_API_TOKEN": Variable(
        secret=True,
        required=True,
        generated_bytes=32,
        rotation_services=("api", "bot", "worker"),
    ),
    "PAYMENT_PROVIDER": Variable(required=True),
    "PAYMENT_WEBHOOK_SECRET": Variable(
        secret=True,
        required=True,
        rotation_services=("api", "worker"),
        requires_external_sync=True,
    ),
    "PAYMENT_AUTO_CONFIRM": Variable(required=True),
    "PROMO_CODES": Variable(),
    "ADMIN_USERNAME": Variable(required=True),
    "ADMIN_PASSWORD": Variable(
        secret=True,
        required=True,
        generated_bytes=24,
        rotation_services=("api",),
    ),
    "BACKGROUND_JOBS_ENABLED": Variable(required=True),
    "LIFECYCLE_INTERVAL_SECONDS": Variable(),
    "LIFECYCLE_ADVISORY_LOCK_KEY": Variable(),
    "WORKER_RUN_ONCE": Variable(),
    "CABINET_EMAIL_CODE_TTL_MINUTES": Variable(),
    # Этот bearer выдаёт мастер 3x-ui. Локально сгенерированное значение там
    # неизвестно и лишь оборвёт синхронизацию, поэтому оно не ротируется здесь.
    "THREEXUI_API_TOKEN": Variable(secret=True, required=True),
    "THREEXUI_VERIFY_TLS": Variable(required=True),
}


class EnvFile:
    """Сохранять комментарии и порядок при безопасном изменении dotenv-файла."""

    def __init__(self, path: Path):
        self.path = path
        self.lines = path.read_text().splitlines() if path.exists() else []

    def values(self) -> dict[str, str]:
        """Вернуть последнее значение каждой простой записи KEY=VALUE."""
        result: dict[str, str] = {}
        for line in self.lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            result[key] = value
        return result

    def set(self, key: str, value: str) -> None:
        """Заменить первую совпавшую запись или добавить новую."""
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
        """Атомарно заменить файл, оставив доступ только владельцу."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                delete=False,
            ) as temporary:
                temporary_name = temporary.name
                temporary.write("\n".join(self.lines).rstrip() + "\n")
                temporary.flush()
                os.fsync(temporary.fileno())
            os.chmod(temporary_name, stat.S_IRUSR | stat.S_IWUSR)
            os.replace(temporary_name, self.path)
        except Exception:
            if temporary_name:
                Path(temporary_name).unlink(missing_ok=True)
            raise


def mask(value: str) -> str:
    """Показать достаточно символов для узнавания секрета, не раскрывая его."""
    if not value:
        return "<empty>"
    if len(value) <= 8:
        return "********"
    return f"{value[:4]}…{value[-4:]}"


def validate(values: dict[str, str]) -> list[str]:
    """Вернуть за один проход все требующие исправления ошибки конфигурации."""
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
    return errors


def generated_value(metadata: Variable) -> str:
    """Выпустить значение только для секрета, которым владеет этот проект."""
    if metadata.generated_bytes is None or not metadata.rotation_services:
        raise ValueError("This variable cannot be generated locally")
    return secrets.token_urlsafe(metadata.generated_bytes)


def internal_rotatable_keys() -> tuple[str, ...]:
    """Вернуть локальные секреты без внешней стороны, с которой надо синхронизироваться."""
    return tuple(
        key
        for key, metadata in VARIABLES.items()
        if metadata.generated_bytes is not None
        and metadata.rotation_services
        and not metadata.requires_external_sync
    )


def rotation_services(keys: list[str]) -> list[str]:
    """Определить минимальный набор Compose-сервисов для согласованной ротации."""
    selected = {
        service
        for key in keys
        for service in VARIABLES[key].rotation_services
    }
    preferred_order = ("api", "bot", "worker")
    return [service for service in preferred_order if service in selected] + sorted(
        selected - set(preferred_order)
    )


def apply_services(env_file: Path, services: list[str]) -> None:
    """Пересоздать сервисы с текущим .env без shell-интерпретации аргументов."""
    project_directory = env_file.resolve().parent
    command = [
        "docker",
        "compose",
        "--project-directory",
        str(project_directory),
        "--env-file",
        str(env_file.resolve()),
        "up",
        "-d",
        "--force-recreate",
        *services,
    ]
    subprocess.run(command, check=True)


def rotate(
    env: EnvFile,
    keys: list[str],
    *,
    dry_run: bool,
) -> int:
    """Атомарно заменить несколько локальных секретов и применить их к сервисам."""
    if not keys:
        raise ValueError("Specify variables to rotate or use --all-internal")
    if len(set(keys)) != len(keys):
        raise ValueError("Each variable may be listed only once")

    external_sync = [
        key
        for key in keys
        if key in VARIABLES and VARIABLES[key].requires_external_sync
    ]
    if external_sync:
        raise ValueError(
            "Local rotation is not supported for: " + ", ".join(external_sync)
            + ". Change it at the external provider, then use configctl set and apply."
        )

    unsupported = [
        key
        for key in keys
        if key not in VARIABLES
        or VARIABLES[key].generated_bytes is None
        or not VARIABLES[key].rotation_services
    ]
    if unsupported:
        raise ValueError(
            "Local rotation is not supported for: " + ", ".join(sorted(unsupported))
        )

    replacements = {key: generated_value(VARIABLES[key]) for key in keys}
    candidate = env.values() | replacements
    errors = validate(candidate)
    if errors:
        raise ValueError("configuration would be invalid: " + "; ".join(errors))

    services = rotation_services(keys)
    if dry_run:
        print("would rotate " + ", ".join(keys))
        print("would recreate " + ", ".join(services))
        return 0

    previous_lines = list(env.lines)
    for key, value in replacements.items():
        env.set(key, value)
    env.save()

    try:
        apply_services(env.path, services)
    except (OSError, subprocess.CalledProcessError) as error:
        env.lines = previous_lines
        env.save()
        try:
            apply_services(env.path, services)
        except (OSError, subprocess.CalledProcessError) as rollback_error:
            print(
                "rotation failed; .env was restored but service rollback also failed: "
                f"{rollback_error}",
            )
            return 1
        print(f"rotation failed; .env and services were restored: {error}")
        return 1

    print("rotated " + ", ".join(keys))
    print("recreated " + ", ".join(services))
    return 0


def parser() -> argparse.ArgumentParser:
    """Построить грамматику CLI для ручного и автоматизированного запуска."""
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
    rotate_parser = subparsers.add_parser("rotate")
    rotate_parser.add_argument("keys", nargs="*")
    rotate_parser.add_argument(
        "--all-internal",
        action="store_true",
        help="rotate all secrets managed only by this project",
    )
    rotate_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show variables and services without changing .env or Docker",
    )
    subparsers.add_parser("validate")
    apply_parser = subparsers.add_parser("apply")
    apply_parser.add_argument(
        "--services",
        nargs="+",
        default=["api", "bot", "worker"],
    )
    return result


def main() -> int:
    """Выполнить команду configctl и вернуть код завершения процесса."""
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
        if metadata is None:
            raise SystemExit(f"Generation is not supported for {args.key}")
        try:
            value = generated_value(metadata)
        except ValueError:
            raise SystemExit(f"Generation is not supported for {args.key}") from None
        env.set(args.key, value)
        env.save()
        print(f"generated {args.key}")
        return 0

    if args.command == "rotate":
        if args.all_internal and args.keys:
            raise SystemExit("Use either explicit variables or --all-internal, not both")
        keys = list(internal_rotatable_keys()) if args.all_internal else args.keys
        try:
            return rotate(
                env,
                keys,
                dry_run=args.dry_run,
            )
        except ValueError as error:
            raise SystemExit(str(error)) from None

    if args.command == "validate":
        errors = validate(values)
        if errors:
            for error in errors:
                print(f"ERROR {error}")
            return 1
        print("configuration is valid")
        return 0

    # Пересоздание сервисов с неверной конфигурацией может вызвать простой,
    # поэтому `apply` всегда выполняет проверку перед запуском Docker Compose.
    errors = validate(values)
    if errors:
        raise SystemExit("configuration is invalid; run configctl validate")
    # Передаём аргументы списком, а не shell-строкой, чтобы пути и имена сервисов
    # не могли быть повторно интерпретированы как синтаксис shell.
    apply_services(args.env_file, args.services)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
