import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_bot_does_not_recommend_icmp_as_vless_healthcheck() -> None:
    with (ROOT / "bot/app/content.json").open(encoding="utf-8") as source:
        support = json.load(source)["texts"]["support"]

    assert "ping использует ICMP" in support
    assert "Откройте сайт в браузере" in support


def test_operations_guide_documents_ssl_renew_scripts() -> None:
    guide = (ROOT / "docs/maintenance-scripts.md").read_text(encoding="utf-8")

    assert "scripts/renew_master_cert.sh" in guide
    assert "deploy/node/renew_3xui_ip_cert.sh" in guide
