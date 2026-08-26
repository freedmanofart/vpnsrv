import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_bot_does_not_recommend_icmp_as_vless_healthcheck() -> None:
    with (ROOT / "bot/app/content.json").open(encoding="utf-8") as source:
        instructions = json.load(source)["texts"]["instructions"]

    assert "ping использует ICMP" in instructions
    assert "откройте сайт в браузере" in instructions


def test_operations_guide_uses_https_egress_checks() -> None:
    guide = (ROOT / "docs/maintenance-scripts.md").read_text(encoding="utf-8")

    assert "curl --fail --show-error --max-time 15 https://api.ipify.org" in guide
    assert "tail -n 100 /var/log/vpn-xray/access.log" in guide
