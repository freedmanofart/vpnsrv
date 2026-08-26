from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (ROOT / "scripts/harden_vpn_node_firewall.sh").read_text(encoding="utf-8")


def test_firewall_is_default_deny_and_keeps_vpn_public() -> None:
    assert '--set-target=DROP' in SCRIPT
    assert 'XRAY_TCP_PORTS=${XRAY_TCP_PORTS:-443}' in SCRIPT
    assert '--add-port="$port/tcp"' in SCRIPT
    assert "SSH_ACCESS_MODE=${SSH_ACCESS_MODE:-key-only}" in SCRIPT
    assert 'SSH_ACCESS_MODE == key-only' in SCRIPT
    assert '--add-port="$SSH_PORT/tcp"' in SCRIPT
    assert "PasswordAuthentication no" in SCRIPT
    assert "KbdInteractiveAuthentication no" in SCRIPT
    assert "PubkeyAuthentication yes" in SCRIPT
    assert "SSH_ALLOW_CIDRS is required in SSH_ACCESS_MODE=cidr" in SCRIPT
    assert "Refusing a world-open SSH CIDR" in SCRIPT


def test_firewall_has_automatic_rollback_and_requires_new_session() -> None:
    assert 'systemd-run --unit="$ROLLBACK_UNIT"' in SCRIPT
    assert '"$SELF_PATH" --rollback' in SCRIPT
    assert '${SSH_CONNECTION} != "$apply_connection"' in SCRIPT
    assert "DO NOT CLOSE THIS SESSION" in SCRIPT
    assert "--confirm" in SCRIPT


def test_copy_helper_does_not_apply_firewall() -> None:
    helper = (ROOT / "scripts/copy_vpn_node_firewall.sh").read_text(encoding="utf-8")
    assert "scp -o BatchMode=yes" in helper
    assert "chmod 0700" in helper
    assert "firewall was NOT applied" in helper
