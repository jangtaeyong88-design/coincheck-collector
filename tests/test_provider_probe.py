import unittest
from io import BytesIO
from urllib.error import HTTPError

from collector.provider_probe import _matched, probe, request


class ProviderProbeTests(unittest.TestCase):
    def test_http_denial_records_code_without_retry(self):
        class Denied:
            calls = 0
            def open(self, req, timeout):
                self.calls += 1
                raise HTTPError(req.full_url, 403, "Forbidden", {},
                                BytesIO(b'{"msg":"access blocked from your country"}'))
        opener = Denied()
        evidence, payload = request("bitget", "/api/v3/market/instruments",
                                    opener=opener, sleeper=lambda _: None)
        self.assertEqual(evidence["http_status"], 403)
        self.assertEqual(evidence["status"], "FETCH_FAILED")
        self.assertIn("country", evidence["reason"])
        self.assertIsNone(payload)
        self.assertEqual(opener.calls, 1)

    def test_contract_metadata_is_required(self):
        valid = {"instId": "BTC-USDT-SWAP", "instType": "SWAP", "state": "live",
                 "ctType": "linear", "settleCcy": "USDT", "ctValCcy": "BTC"}
        self.assertEqual(_matched("okx", "BTC", [valid]), valid)
        self.assertIsNone(_matched("okx", "BTC", [{**valid, "ctValCcy": "1000BTC"}]))
        bitget = {"symbol": "BTCUSDT", "category": "USDT-FUTURES",
                  "baseCoin": "BTC", "quoteCoin": "USDT", "type": "perpetual",
                  "status": "online", "symbolType": "crypto"}
        self.assertEqual(_matched("bitget", "BTC", [bitget]), bitget)
        self.assertIsNone(_matched("bitget", "BTC", [{**bitget, "type": "delivery"}]))

    def test_probe_keeps_unsupported_data_separate(self):
        calls = []
        def getter(exchange, path, params=None):
            calls.append((exchange, path, params))
            if path.endswith("/instruments"):
                if exchange == "okx":
                    data = [{"instId": f"{code}-USDT-SWAP", "instType": "SWAP",
                             "state": "live", "ctType": "linear", "settleCcy": "USDT",
                             "ctValCcy": code} for code in ("BTC", "ETH", "SOL")]
                else:
                    data = [{"symbol": f"{code}USDT", "category": "USDT-FUTURES",
                             "baseCoin": code, "quoteCoin": "USDT", "type": "perpetual",
                             "status": "online", "symbolType": "crypto"}
                            for code in ("BTC", "ETH", "SOL")]
            else:
                data = [{"value": "1", "ts": "1000"}]
            return {"path": path, "http_status": 200, "api_success": True,
                    "api_code": "0" if exchange == "okx" else "00000",
                    "status": "AVAILABLE", "reason": None}, {"data": data}
        result = probe(getter=getter, sleeper=lambda _: None)
        self.assertEqual(result["exchanges"]["okx"]["markets"]["BTC"]["match_status"],
                         "VERIFIED")
        bitget = result["exchanges"]["bitget"]["markets"]["BTC"]
        self.assertEqual(bitget["checks"]["oi_history"]["status"], "NOT_SUPPORTED")
        self.assertEqual(bitget["checks"]["top_accounts"]["status"], "NOT_SUPPORTED")
        self.assertFalse(any(exchange == "bitget" and "oi-history" in path
                             for exchange, path, _ in calls))


if __name__ == "__main__":
    unittest.main()
