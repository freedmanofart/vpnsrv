#!/usr/bin/env python3
"""Queue commit/assistant events and deliver them through the production API."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def outbox_path(root: Path) -> Path:
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--git-path", "freedomvpn-admin-alert-outbox.jsonl"],
        check=True,
        capture_output=True,
        text=True,
    )
    path = Path(result.stdout.strip())
    return path if path.is_absolute() else root / path


def commit_event(root: Path) -> dict:
    sha = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    metadata = subprocess.run(
        ["git", "-C", str(root), "show", "-s", "--format=%an%x1f%s", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    author, _, subject = metadata.partition("\x1f")
    branch = subprocess.run(
        ["git", "-C", str(root), "branch", "--show-current"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip() or "detached"
    files = subprocess.run(
        ["git", "-C", str(root), "show", "--format=", "--name-only", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    files = [path.strip() for path in files if path.strip()]
    return {
        "type": "commit",
        "sha": sha,
        "branch": branch,
        "author": author,
        "subject": subject,
        "files": files[:12],
        "files_total": len(files),
    }


def assistant_event(message: str) -> dict:
    return {"type": "assistant_message", "message": message}


def test_event(kind: str, message: str) -> dict:
    kinds = {
        "test-health": "test_health",
        "test-audit": "test_audit",
        "test-commit": "test_commit",
        "test-assistant": "test_assistant",
    }
    if kind not in kinds:
        raise ValueError(f"unsupported event mode: {kind}")
    return {"type": kinds[kind], "message": message}


def deliver(root: Path, event: dict) -> bool:
    if str(root).startswith("/home/freedman/vpn-service"):
        command = ["docker", "exec", "-i", "vpn-api", "python", "-m", "app.admin_alert_cli"]
    else:
        target = os.environ.get("FREEDOMVPN_ALERT_SSH_TARGET", "root@192.168.10.244")
        command = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=8",
            "-o",
            "HostKeyAlias=freedomvpn",
            target,
            "docker",
            "exec",
            "-i",
            "vpn-api",
            "python",
            "-m",
            "app.admin_alert_cli",
        ]
    result = subprocess.run(
        command,
        input=json.dumps(event, ensure_ascii=False) + "\n",
        capture_output=True,
        text=True,
        timeout=35,
        check=False,
    )
    if result.returncode != 0:
        if result.stderr.strip():
            print(result.stderr.strip()[:600], file=sys.stderr)
        return False
    return True


def write_outbox(path: Path, events: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix="admin-alert-outbox-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as temp_file:
            for event in events:
                temp_file.write(json.dumps(event, ensure_ascii=False) + "\n")
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def run() -> int:
    root = repo_root()
    if len(sys.argv) < 2:
        print("usage: notify_admin_event.py commit|assistant|test-*", file=sys.stderr)
        return 2
    mode = sys.argv[1]
    try:
        if mode == "commit":
            new_event = commit_event(root)
        elif mode == "assistant":
            message = sys.stdin.read().strip()
            if not message:
                raise ValueError("assistant message must be provided on stdin")
            new_event = assistant_event(message)
        elif mode.startswith("test-"):
            message = " ".join(sys.argv[2:]).strip() or "Проверка доставки в Freedom VPN Admin."
            new_event = test_event(mode, message)
        else:
            raise ValueError(f"unsupported event mode: {mode}")
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    path = outbox_path(root)
    pending: list[dict] = []
    if path.is_file():
        try:
            pending = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        except (OSError, json.JSONDecodeError):
            print(f"cannot read admin alert outbox: {path}", file=sys.stderr)
            return 1
    pending.append(new_event)
    remaining = []
    for index, event in enumerate(pending):
        if not deliver(root, event):
            remaining = pending[index:]
            break
    write_outbox(path, remaining)
    if remaining:
        print(f"admin Telegram alert queued for retry ({len(remaining)} pending)", file=sys.stderr)
        return 1 if mode.startswith("test-") or mode == "assistant" else 0
    print("delivered")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
