from pathlib import Path
import subprocess


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/run_standalone_xray_node.sh"


def test_standalone_node_has_no_control_plane_dependency() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert "CONTROL_PLANE_URL" not in source
    assert "NODE_AGENT_TOKEN" not in source
    assert '"clients": [{"id": client_id' in source


def test_standalone_uri_uses_compatible_reality_tcp_without_vision() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    uri = next(line for line in source.splitlines() if line.startswith('URI="vless://'))
    assert "type=tcp" in uri
    assert "security=reality" in uri
    assert "fp=chrome" in uri
    assert "flow=" not in uri


def test_standalone_script_does_not_stop_existing_listener() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert "never stops production services automatically" in source
    assert 'ss -H -lnt "sport = :$XRAY_PORT"' in source


def test_config_is_readable_by_xray_uid_before_validation() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    ownership = source.index('chown 65532:65532 "$STATE_DIR/config.json"')
    validation = source.index('run -test -config /config.json')

    assert ownership < validation


def test_runner_verifies_reality_egress_with_an_xray_client() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert '"protocol": "socks"' in source
    assert '"protocol": "vless"' in source
    assert '--proxy "socks5h://127.0.0.1:$SOCKS_PORT"' in source
    assert "Built-in Xray client egress succeeded" in source


def test_runner_explains_rejected_reality_before_vless_access() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert '"--diagnose"' in source
    assert "failed to read client hello" in source
    assert "rejected handshakes never become VLESS" in source
    assert "external client is not speaking Reality" in source
    assert "external_accepted == 0" in source
    assert "127\\.0\\.0\\.1" in source
    assert "handshake did not complete successfully" in source


def test_runner_does_not_treat_mixed_handshakes_as_node_failure() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    mixed = source.index("external_accepted > 0 && invalid > 0")
    rejected_only = source.index("invalid > 0 && external_accepted == 0")
    assert mixed < rejected_only
    assert "the node data plane works" in source
    assert "client's TUN, DNS" in source


def test_runner_rejects_concatenated_diagnose_command() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert "two diagnose commands were pasted without a newline" in source
    assert "Usage: $0 [--diagnose|--remove]" in source

    result = subprocess.run(
        [str(SCRIPT), "--diagnose/root/run_standalone_xray_node.sh", "--diagnose"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "Usage:" in result.stderr


def test_runner_starts_each_access_log_cleanly() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert ': > "$STATE_DIR/log/access.log"' in source
