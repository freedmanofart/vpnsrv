import importlib.util
from pathlib import Path
import unittest


spec = importlib.util.spec_from_file_location(
    "issue",
    Path(__file__).parents[1] / "scripts/issue_provider_code.py",
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class Response:
    def __init__(self, status, body):
        self.status_code = status
        self.body = body

    def json(self):
        return self.body


class Client:
    def __init__(self, response):
        self.response = response

    def post(self, path, json):
        self.path = path
        self.body = json
        return self.response


class IssueCodeTest(unittest.TestCase):
    def test_leading_zero_and_contract(self):
        client = Client(
            Response(
                200,
                {
                    "code": "00123456",
                    "device_name": "dev-a1b2c3",
                    "expires_at": "2030-01-01T00:00:00Z",
                    "secret": "never-output",
                },
            )
        )
        result = module.issue_code(client, 123, 5)
        self.assertEqual(result["code"], "00123456")
        self.assertEqual(result["device_name"], "dev-a1b2c3")
        self.assertEqual(set(result), {"code", "device_name", "expires_at"})
        self.assertEqual(client.path, "/v1/client/activation-codes")
        self.assertEqual(client.body, {"telegram_id": 123, "ttl_minutes": 5})

    def test_error_body_is_not_exposed(self):
        with self.assertRaisesRegex(RuntimeError, "^Code issuance failed: HTTP 403$"):
            module.issue_code(Client(Response(403, {"secret": "secret"})), 123, 5)

    def test_invalid_code(self):
        with self.assertRaises(RuntimeError):
            module.issue_code(Client(Response(200, {"code": "bad"})), 123, 5)


if __name__ == "__main__":
    unittest.main()
