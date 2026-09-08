import json
import unittest
from urllib.parse import parse_qs, urlparse

from app.services.vless import build_vless_url


class VlessURLTests(unittest.TestCase):
    def test_builds_reality_xhttp_mlkem_link(self):
        uri = build_vless_url(
            uuid="9da75a56-2cfd-4f20-8265-7ba4b5576032",
            host="89.127.212.239",
            port=8443,
            remark="vpn-49",
            config={
                "encryption": "mlkem768x25519plus.native.0rtt.key",
                "extra": {"mode": "auto", "xPaddingBytes": "100-1000"},
                "fp": "firefox",
                "xhttp_host": "",
                "mode": "auto",
                "path": "/",
                "pbk": "public-key",
                "security": "reality",
                "sid": "27191c181f5d455d",
                "sni": "duckduckgo.com",
                "spx": "/e4758470b93fa67",
                "type": "xhttp",
                "x_padding_bytes": "100-1000",
            },
        )

        parsed = urlparse(uri)
        query = parse_qs(parsed.query, keep_blank_values=True)
        self.assertEqual(parsed.hostname, "89.127.212.239")
        self.assertEqual(parsed.port, 8443)
        self.assertEqual(parsed.fragment, "vpn-49")
        self.assertEqual(query["encryption"], ["mlkem768x25519plus.native.0rtt.key"])
        self.assertEqual(query["host"], [""])
        self.assertEqual(query["type"], ["xhttp"])
        self.assertEqual(query["x_padding_bytes"], ["100-1000"])
        self.assertEqual(
            json.loads(query["extra"][0]),
            {"mode": "auto", "xPaddingBytes": "100-1000"},
        )


if __name__ == "__main__":
    unittest.main()
