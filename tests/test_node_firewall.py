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
    assert '--setenv="STATE_DIR=$STATE_DIR" --setenv="ZONE=$ZONE"' in SCRIPT
    assert '"$SELF_PATH" --rollback' in SCRIPT
    assert '${SSH_CONNECTION} != "$apply_connection"' in SCRIPT
    assert "DO NOT CLOSE THIS SESSION" in SCRIPT
    assert "--confirm" in SCRIPT


def test_reapply_replaces_old_allow_list() -> None:
    remove_ports = '--remove-port="$port"'
    remove_rules = '--remove-rich-rule="$rule"'
    assert remove_ports in SCRIPT
    assert remove_rules in SCRIPT
    assert SCRIPT.index(remove_ports) < SCRIPT.index('--add-port="$port/tcp"')
    assert SCRIPT.index(remove_rules) < SCRIPT.index('--add-rich-rule=')


def test_rollback_keeps_state_until_restoration_is_verified() -> None:
    verification = 'restored_zone=$(firewall-cmd --get-zone-of-interface="$iface"'
    cleanup = 'rm -rf "$STATE_DIR"'
    assert "rollback state retained" in SCRIPT
    assert verification in SCRIPT
    assert SCRIPT.index(verification) < SCRIPT.index(cleanup)


def test_reapply_rollback_snapshots_and_restores_old_allow_list() -> None:
    snapshot_guard = 'if [[ $OLD_ZONE == "$ZONE" ]]'
    restore_guard = 'if [[ -f $STATE_DIR/restore-zone ]]'
    assert snapshot_guard in SCRIPT
    assert '--list-ports >"$STATE_DIR/zone-ports"' in SCRIPT
    assert '--list-rich-rules >"$STATE_DIR/zone-rich-rules"' in SCRIPT
    assert '--get-target >"$STATE_DIR/zone-target"' in SCRIPT
    assert restore_guard in SCRIPT
    assert '--add-port="$port"' in SCRIPT
    assert '--add-rich-rule="$rule"' in SCRIPT
    assert '--set-target="$old_target"' in SCRIPT
    assert SCRIPT.index(snapshot_guard) < SCRIPT.index('--set-target=DROP')
    assert SCRIPT.index(restore_guard) < SCRIPT.index('firewall-cmd --reload')


def test_copy_helper_does_not_apply_firewall() -> None:
    helper = (ROOT / "scripts/copy_vpn_node_firewall.sh").read_text(encoding="utf-8")
    assert "scp -o BatchMode=yes" in helper
    assert "chmod 0700" in helper
    assert "firewall was NOT applied" in helper
