import unittest
from datetime import UTC, datetime, timedelta

from collector.indicators import (
    calculate_indicators,
    collect_minute_history,
    mark_daily_candles,
)


def candles(count=40, *, daily_gain=1.0, volume=100.0, range_size=4.0):
    result = []
    start = datetime(2026, 1, 1, tzinfo=UTC)
    for index in range(count):
        close = 100 + index * daily_gain
        result.append(
            {
                "candle_date_time_utc": (start + timedelta(days=index)).strftime(
                    "%Y-%m-%dT%H:%M:%S"
                ),
                "opening_price": close - 1,
                "high_price": close + range_size / 2,
                "low_price": close - range_size / 2,
                "trade_price": close,
                "candle_acc_trade_volume": volume,
                "is_complete": True,
            }
        )
    return result


class IndicatorTests(unittest.TestCase):
    def test_price_changes_rvol_atr_and_bollinger_width(self):
        source = candles()
        result = calculate_indicators(source)
        self.assertAlmostEqual(result["price_change_pct"]["1d"], (139 / 138 - 1) * 100)
        self.assertAlmostEqual(result["price_change_pct"]["3d"], (139 / 136 - 1) * 100)
        self.assertAlmostEqual(result["price_change_pct"]["7d"], (139 / 132 - 1) * 100)
        self.assertEqual(result["rvol"], {"1d": 1.0, "3d": 1.0, "7d": 1.0})
        self.assertEqual(result["atr_14"], 4.0)
        self.assertIsNotNone(result["bollinger_band_width_20_pct"])
        self.assertEqual(result["recent_box"]["duration_days"], 9)
        self.assertAlmostEqual(result["distance_from_20d_low_pct"], (139 / 118 - 1) * 100)

    def test_volume_dry_up(self):
        source = candles()
        source[-1]["candle_acc_trade_volume"] = 40
        result = calculate_indicators(source)
        self.assertEqual(result["volume_dry_up"]["ratio_to_prior_20d"], 0.4)
        self.assertTrue(result["volume_dry_up"]["is_dry_up"])

    def test_insufficient_data_is_null(self):
        result = calculate_indicators(candles(4))
        self.assertIsNone(result["price_change_pct"]["7d"])
        self.assertIsNone(result["rvol"]["1d"])
        self.assertIsNone(result["atr_14"])
        self.assertIsNone(result["bollinger_band_width_20_pct"])
        self.assertIsNone(result["recent_box"]["duration_days"])

    def test_current_daily_candle_is_excluded(self):
        newest_first = list(reversed(candles(2)))
        marked = mark_daily_candles(newest_first, datetime(2026, 1, 2, 12, tzinfo=UTC))
        self.assertTrue(marked[0]["is_complete"])
        self.assertFalse(marked[1]["is_complete"])
        self.assertEqual(calculate_indicators(marked)["completed_candle_count"], 1)

    def test_minute_history_is_paged_and_sorted(self):
        class FakeClient:
            def __init__(self):
                self.calls = []

            def get_minute_candles(self, market, unit, count, to=None):
                self.calls.append((market, unit, count, to))
                newest = 250 if to is None else 50
                return [
                    {"candle_date_time_utc": f"2026-01-01T00:{value:02d}:00"}
                    for value in range(newest, newest - count, -1)
                ]

        client = FakeClient()
        result = collect_minute_history(client, "KRW-BTC", 1, 250)
        self.assertEqual(len(result), 250)
        self.assertEqual([call[2] for call in client.calls], [200, 50])
        self.assertIsNotNone(client.calls[1][3])


if __name__ == "__main__":
    unittest.main()
