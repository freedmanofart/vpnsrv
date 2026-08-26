from pathlib import Path


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
