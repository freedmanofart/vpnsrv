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


def test_node_ip_certificate_renewal_uses_shortlived_profile() -> None:
    script = (ROOT / "deploy/node/renew_3xui_ip_cert.sh").read_text(encoding="utf-8")
    timer = (ROOT / "deploy/systemd/vpn-3xui-ip-cert-renew.timer").read_text(encoding="utf-8")

    assert script.count("--certificate-profile shortlived") == 2
    assert script.count("--days 3") == 2
    assert "systemctl restart x-ui && systemctl reload nginx" in script
    assert "x-ui setting" not in script
    renew_command = script.split("--renew", 1)[1].split("; then", 1)[0]
    assert "renew_required=1" in script
    assert "-checkend" in script
    assert "current certificate has more than 3 days remaining" in script
    assert "skipping issuance\"\n  exit 0" in script
    assert "--force" in renew_command
    assert "OnUnitActiveSec=2d" in timer
