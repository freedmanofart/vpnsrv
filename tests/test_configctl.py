import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "configctl.py"
SPEC = importlib.util.spec_from_file_location("configctl", MODULE_PATH)
configctl = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = configctl
SPEC.loader.exec_module(configctl)


class ConfigCtlTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
