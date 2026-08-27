from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def test_complete_toolkit_is_present() -> None:
    for name in (
        "copy_amneziawg_node_test.sh",
        "copy_standalone_amneziawg_node.sh",
        "install_amneziawg_node_dependencies.sh",
        "run_standalone_amneziawg_node.sh",
    ):
        path = ROOT / "scripts" / name
        assert path.is_file()
        subprocess.run(
            ["git", "ls-files", "--error-unmatch", str(path.relative_to(ROOT))],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )


def test_copy_helper_is_safe_and_does_not_run_remote_tools() -> None:
    source = (ROOT / "scripts/copy_amneziawg_node_test.sh").read_text()
    assert 'test -f "$RUNNER_SOURCE"' in source
    assert 'test -f "$INSTALLER_SOURCE"' in source
    assert "chmod 0700" in source
    assert "INSTALL_AWG=1 $INSTALLER_REMOTE_PATH" in source
    assert "Nothing was installed or started" in source


def test_installer_requires_confirmation_and_official_copr() -> None:
    source = (ROOT / "scripts/install_amneziawg_node_dependencies.sh").read_text()
    assert "${INSTALL_AWG:-0} == 1" in source
    assert "amneziavpn/amneziawg" in source
    assert "amneziawg-dkms amneziawg-tools" in source
    assert 'kernel_devel="kernel-devel-$kernel_release"' in source
    assert 'rpm -q "$kernel_devel"' in source
    assert "Update/reboot into a supported kernel" in source


def test_alias_delegates_to_primary_helper() -> None:
    source = (ROOT / "scripts/copy_standalone_amneziawg_node.sh").read_text()
    assert 'exec "$SCRIPT_DIR/copy_amneziawg_node_test.sh" "$@"' in source
