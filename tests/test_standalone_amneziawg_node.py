from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/run_standalone_amneziawg_node.sh"


def test_amneziawg_test_is_independent_and_uses_official_tools() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert "CONTROL_PLANE" not in source
    assert "NODE_AGENT" not in source
    assert "command -v awg" in source
    assert "command -v awg-quick" in source
    assert 'awg-quick up "$CONFIG"' in source


def test_amneziawg_client_and_server_share_obfuscation() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    for option in ("Jc", "Jmin", "Jmax", "S1", "S2", "H1", "H2", "H3", "H4"):
        assert source.count(f"{option} = ${option}") == 2
    assert "PresharedKey = $PRESHARED_KEY" in source
    assert "AllowedIPs = 0.0.0.0/0" in source


def test_amneziawg_test_configures_forwarding_firewall_and_status() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert "net.ipv4.ip_forward=1" in source
    assert "ip-forward-original.txt" in source
    assert '--add-port="$AWG_PORT/udp"' in source
    assert "port-added.txt" in source
    assert '--add-masquerade' in source
    assert '--remove-masquerade' in source
    assert '--zone=trusted --add-interface="$INTERFACE"' in source
    assert "trap cleanup_failed_start ERR" in source
    assert 'awg show "$INTERFACE"' in source
    assert "tcpdump -ni any udp port $AWG_PORT" in source
