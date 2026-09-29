"""Opt-in read-only checks against the official Binance and Bybit public APIs."""

import os
import unittest

from collector.derivatives import build_derivatives_snapshot, default_clients, match_contract


@unittest.skipUnless(os.getenv("RUN_DERIVATIVES_INTEGRATION") == "1",
                     "official exchange API test is opt-in")
class DerivativesIntegrationTests(unittest.TestCase):
    def test_btc_public_catalog_and_oi(self):
        binance, bybit = default_clients()
        info = binance.get("/fapi/v1/exchangeInfo")
        match = match_contract("KRW-BTC", {"english_name": "Bitcoin"},
                               info["symbols"], "binance")
        self.assertEqual(match["status"], "VERIFIED")
        oi = binance.get("/fapi/v1/openInterest", {"symbol": match["symbol"]})
        self.assertGreater(float(oi["openInterest"]), 0)
        page = bybit.get("/v5/market/instruments-info", {"category": "linear",
                                                           "symbol": "BTCUSDT"})
        match = match_contract("KRW-BTC", {"english_name": "Bitcoin"},
                               page["result"]["list"], "bybit")
        self.assertEqual(match["status"], "VERIFIED")
        ticker = bybit.get("/v5/market/tickers", {"category": "linear", "symbol": "BTCUSDT"})
        self.assertGreater(float(ticker["result"]["list"][0]["openInterest"]), 0)

    def test_btc_collector_supported_public_fields(self):
        binance, bybit = default_clients()
        latest = {"markets": [{"market": "KRW-BTC",
                               "metadata": {"english_name": "Bitcoin"}}]}
        result = build_derivatives_snapshot(latest, binance, bybit)
        btc = result["markets"]["KRW-BTC"]
        for exchange in ("binance", "bybit"):
            with self.subTest(exchange=exchange):
                record = btc[exchange]
                self.assertEqual(record["match"]["status"], "VERIFIED")
                self.assertGreater(record["oi"]["exchange_quantity"], 0)
                self.assertIsNotNone(record["oi"]["at_ms"])
                self.assertIsNotNone(record["funding"]["latest_rate"])
                self.assertIsNotNone(record["long_short"]["general_accounts"]["ratio"])
                self.assertEqual(record["errors"], [])


if __name__ == "__main__":
    unittest.main()
