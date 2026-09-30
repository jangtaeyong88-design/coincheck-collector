"""Opt-in tests against the real Upbit API (RUN_UPBIT_INTEGRATION=1)."""

import os
import unittest
from datetime import UTC, datetime

from collector.indicators import calculate_indicators, mark_daily_candles
from collector.upbit import UpbitClient


@unittest.skipUnless(os.getenv("RUN_UPBIT_INTEGRATION") == "1", "real API test is opt-in")
class UpbitIntegrationTests(unittest.TestCase):
    def test_btc_eth_xrp_sol_history_and_indicators(self):
        client = UpbitClient()
        listed = {item["market"] for item in client.get_krw_markets()}
        for symbol in ("BTC", "ETH", "XRP", "SOL"):
            market = f"KRW-{symbol}"
            with self.subTest(market=market):
                self.assertIn(market, listed)
                candles = mark_daily_candles(
                    client.get_daily_candles(market, 60), datetime.now(UTC)
                )
                self.assertGreaterEqual(len(candles), 20)
                indicators = calculate_indicators(candles)
                self.assertIsNotNone(indicators["atr_14"])
                self.assertIsNotNone(indicators["bollinger_band_width_20_pct"])


if __name__ == "__main__":
    unittest.main()
