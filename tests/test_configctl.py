import importlib.util
import io
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "configctl.py"
SPEC = importlib.util.spec_from_file_location("configctl", MODULE_PATH)
configctl = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = configctl
SPEC.loader.exec_module(configctl)


class ConfigCtlTests(unittest.TestCase):
    def valid_env(self, path: Path) -> dict[str, str]:
        values = {
            key: "value"
            for key, item in configctl.VARIABLES.items()
            if item.required
        }
        values.update(
            {
                "PAYMENT_PROVIDER": "mock",
                "PAYMENT_AUTO_CONFIRM": "false",
                "SERVICE_API_TOKEN": "old-service-token",
                "PAYMENT_WEBHOOK_SECRET": "provider-webhook-secret",
                "ADMIN_PASSWORD": "old-admin-password",
                "THREEXUI_API_TOKEN": "master-issued-token",
            }
        )
        path.write_text("\n".join(f"{key}={value}" for key, value in values.items()) + "\n")
        return values

    def run_cli(self, *arguments: str) -> tuple[int, str]:
        output = io.StringIO()
        with patch.object(sys, "argv", ["configctl", *arguments]), redirect_stdout(output):
            result = configctl.main()
        return result, output.getvalue()

    def test_preserves_comments_and_masks_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("# title\nSERVICE_API_TOKEN=abcdefghijklmnop\nLOG_LEVEL=INFO\n")
            env = configctl.EnvFile(path)
            env.set("LOG_LEVEL", "DEBUG")
            env.set("PAYMENT_PROVIDER", "mock")
            env.save()
            self.assertIn("# title", path.read_text())
            self.assertIn("LOG_LEVEL=DEBUG", path.read_text())
            self.assertEqual("abcd…mnop", configctl.mask(env.values()["SERVICE_API_TOKEN"]))
            self.assertEqual(0o600, path.stat().st_mode & 0o777)

    def test_validation_rejects_placeholder_and_unsafe_auto_confirm(self):
        values = {
            key: "value" for key, item in configctl.VARIABLES.items() if item.required
        }
        values["ADMIN_PASSWORD"] = "change_me"
        values["PAYMENT_PROVIDER"] = "real"
        values["PAYMENT_AUTO_CONFIRM"] = "true"
        errors = configctl.validate(values)
        self.assertIn("ADMIN_PASSWORD: placeholder value", errors)
        self.assertTrue(any("PAYMENT_AUTO_CONFIRM" in error for error in errors))

    def test_rotate_all_internal_updates_all_dependent_services_without_printing_values(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            before = self.valid_env(path)
            with patch.object(configctl, "apply_services") as apply_services:
                result, output = self.run_cli(
                    "--env-file", str(path), "rotate", "--all-internal"
                )

            after = configctl.EnvFile(path).values()
            self.assertEqual(0, result)
            self.assertNotEqual(before["SERVICE_API_TOKEN"], after["SERVICE_API_TOKEN"])
            self.assertNotEqual(before["ADMIN_PASSWORD"], after["ADMIN_PASSWORD"])
            self.assertEqual(before["PAYMENT_WEBHOOK_SECRET"], after["PAYMENT_WEBHOOK_SECRET"])
            apply_services.assert_called_once_with(path, ["api", "bot", "worker"])
            self.assertIn("rotated SERVICE_API_TOKEN, ADMIN_PASSWORD", output)
            self.assertNotIn(after["SERVICE_API_TOKEN"], output)
            self.assertNotIn(after["ADMIN_PASSWORD"], output)

    def test_rotate_dry_run_does_not_change_env_or_restart_services(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            before = self.valid_env(path)
            with patch.object(configctl, "apply_services") as apply_services:
                result, output = self.run_cli(
                    "--env-file", str(path), "rotate", "--all-internal", "--dry-run"
                )

            self.assertEqual(0, result)
            self.assertEqual(before, configctl.EnvFile(path).values())
            apply_services.assert_not_called()
            self.assertIn("would recreate api, bot, worker", output)

    def test_payment_webhook_rotation_is_rejected_until_provider_change_is_done(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            self.valid_env(path)
            with self.assertRaisesRegex(SystemExit, "Change it at the external provider"):
                self.run_cli(
                    "--env-file", str(path), "rotate", "PAYMENT_WEBHOOK_SECRET"
                )

    def test_rotate_restores_previous_env_after_service_apply_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            before = self.valid_env(path)
            failure = subprocess.CalledProcessError(1, ["docker", "compose"])
            with patch.object(
                configctl, "apply_services", side_effect=[failure, None]
            ) as apply_services:
                result, output = self.run_cli(
                    "--env-file", str(path), "rotate", "--all-internal"
                )

            self.assertEqual(1, result)
            self.assertEqual(before, configctl.EnvFile(path).values())
            self.assertEqual(2, apply_services.call_count)
            self.assertIn(".env and services were restored", output)


if __name__ == "__main__":
    unittest.main()
