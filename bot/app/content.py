from __future__ import annotations

import json
import os
from pathlib import Path
from string import Template
from typing import Any


CONTENT_FILE = Path(
    os.getenv("BOT_CONTENT_FILE", str(Path(__file__).with_name("content.json")))
)


def _expand(value: Any) -> Any:
    if isinstance(value, str):
        return Template(value).safe_substitute(os.environ)
    if isinstance(value, list):
        return [_expand(item) for item in value]
    if isinstance(value, dict):
        return {key: _expand(item) for key, item in value.items()}
    return value


def load_content(path: Path = CONTENT_FILE) -> dict:
    with path.open(encoding="utf-8") as source:
        return _expand(json.load(source))


CONTENT = load_content()


def text(name: str) -> str:
    return str(CONTENT.get("texts", {}).get(name, ""))


def link(name: str) -> str:
    return str(CONTENT.get("links", {}).get(name, "")).strip()


def platform(platform_id: str) -> dict | None:
    return next(
        (
            item
            for item in CONTENT.get("platforms", [])
            if item.get("id") == platform_id
        ),
        None,
    )
