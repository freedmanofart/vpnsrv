from app.admin_alert_cli import _message


def test_commit_alert_includes_changed_files() -> None:
    message = _message(
        {
            "type": "commit",
            "branch": "deploy/update",
            "sha": "abc123",
            "author": "Codex",
            "subject": "Update client imports",
            "files": ["api/app/services/notifications.py", "docs/notifications.md"],
            "files_total": 2,
        }
    )

    assert "Изменения:" in message
    assert "• api/app/services/notifications.py" in message
    assert "• docs/notifications.md" in message


def test_commit_alert_limits_file_list_and_reports_remainder() -> None:
    message = _message(
        {
            "type": "commit",
            "files": [f"file-{index}.py" for index in range(12)],
            "files_total": 19,
        }
    )

    assert "• file-11.py" in message
    assert "• и ещё 7 файлов" in message
    assert "file-12.py" not in message
