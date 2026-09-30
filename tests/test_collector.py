import json
import tempfile
import unittest
from pathlib import Path

from collector.main import build_snapshot, write_json_atomic
from collector.upbit import UpbitAPIError, collect_market_data


class FakeClient:
    def get_krw_markets(self):
        return [
            {"market": "KRW-BTC", "korean_name": "비트코인"},
            {"market": "KRW-ETH", "korean_name": "이더리움"},
        ]

    def get_tickers(self, markets):
        return [{"market": market, "trade_price": 1} for market in markets]

    def get_daily_candle(self, market):
        if market == "KRW-ETH":
            raise UpbitAPIError("rate limited")
        return {"market": market, "opening_price": 1, "candle_acc_trade_volume": 2}


class BatchFailureClient(FakeClient):
    def get_tickers(self, markets):
        markets = list(markets)
        if len(markets) > 1 or markets == ["KRW-ETH"]:
            raise UpbitAPIError("ticker unavailable")
        return super().get_tickers(markets)


class CollectorTests(unittest.TestCase):
    def test_partial_candle_failure_is_recorded(self):
        result = collect_market_data(FakeClient())
        self.assertEqual(result["market_count"], 2)
        eth = next(item for item in result["markets"] if item["market"] == "KRW-ETH")
        self.assertIsNone(eth["daily_candle"])
        self.assertEqual(eth["errors"][0]["stage"], "daily_candle")

    def test_failed_ticker_batch_falls_back_and_isolates_market_error(self):
        result = collect_market_data(BatchFailureClient())
        btc, eth = result["markets"]
        self.assertIsNotNone(btc["ticker"])
        self.assertIsNone(eth["ticker"])
        self.assertIn("ticker unavailable", eth["errors"][0]["reason"])

    def test_full_market_snapshot_and_atomic_json_output(self):
        snapshot = build_snapshot(FakeClient())
        self.assertEqual(2, snapshot["summary"]["market_count"])
        self.assertEqual(1, snapshot["summary"]["failed_market_count"])
        self.assertEqual({"KRW-BTC", "KRW-ETH"},
                         {row["market"] for row in snapshot["markets"]})
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "data" / "latest.json"
            write_json_atomic(snapshot, target)
            self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["schema_version"], "1.0")


if __name__ == "__main__":
    unittest.main()
