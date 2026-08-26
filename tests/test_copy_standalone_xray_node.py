from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/copy_standalone_xray_node.sh"


def test_copy_script_requires_explicit_node() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert "NODE_SSH=${NODE_SSH:?" in source
    assert "BatchMode=yes" in source


def test_copy_script_does_not_start_xray_remotely() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    remote_commands = [line for line in source.splitlines() if line.startswith("ssh ")]
    assert len(remote_commands) == 2
    assert all("podman" not in command for command in remote_commands)
    assert all("run_standalone" not in command for command in remote_commands)


def test_copy_script_restricts_remote_path() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert "REMOTE_PATH must be an absolute path without spaces" in source
    assert 'chmod 700 \'$REMOTE_PATH\'' in source
