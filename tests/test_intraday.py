"""Official hourly-candle semantics with synthetic observations only."""

import unittest
from datetime import UTC, datetime, timedelta

from collector.intraday import calculate_hourly, collect, select_targets, calculate_four_hour, collect_four_hour
from collector.upbit import UpbitAPIError


NOW = datetime(2026, 9, 27, 12, 30, tzinfo=UTC)


def hours(count=120, market="KRW-BTC"):
    first = NOW.replace(minute=0) - timedelta(hours=count)
    return [{"market": market,
             "candle_date_time_utc": (first + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M:%S"),
             "trade_price": 100 + i, "candle_acc_trade_volume": 10,
             "candle_acc_trade_price": 1000} for i in range(count)]


class IntradayTests(unittest.TestCase):
    def test_completed_hourly_windows_and_nonoverlapping_baselines(self):
        candles = hours()
        candles[-1]["candle_acc_trade_volume"] = 30
        candles[-1]["candle_acc_trade_price"] = 3000
        candles.append({**candles[-1], "candle_date_time_utc": "2026-09-27T12:00:00",
                        "trade_price": 1000000})  # still open; must not affect metrics
        result = calculate_hourly(candles, NOW, "KRW-BTC")
        self.assertEqual("AVAILABLE", result["status"])
        self.assertEqual("2026-09-27T12:00:00Z", result["observed_at"])
        self.assertEqual(3.0, result["rvol_1h"])
        self.assertEqual(1.5, result["rvol_4h"])
        self.assertEqual(3.0, result["relative_traded_value_1h"])
        self.assertEqual(1.5, result["relative_traded_value_4h"])
        self.assertAlmostEqual((219 / 218 - 1) * 100, result["price_change_pct_1h"], places=5)
        self.assertEqual(3000.0, result["traded_value_1h_krw"])

    def test_missing_hour_is_not_zero_filled_or_compared_as_adjacent(self):
        candles = hours()
        del candles[-2]
        result = calculate_hourly(candles, NOW, "KRW-BTC")
        self.assertEqual("INSUFFICIENT_HISTORY", result["status"])
        self.assertIsNone(result["rvol_1h"])
        self.assertIsNone(result["relative_traded_value_1h"])
        self.assertIsNone(result["price_change_pct_4h"])

    def test_stale_hour_has_no_current_values(self):
        result = calculate_hourly(hours()[:-4], NOW, "KRW-BTC")
        self.assertEqual("STALE", result["status"])
        self.assertIsNone(result["rvol_1h"])

    def test_selection_is_bounded_and_rotated_from_public_tickers(self):
        latest = {"markets": [{"market": f"KRW-X{i:03d}", "ticker": {"trade_price": 100}}
                               for i in range(50)]}
        selected, omitted = select_targets(latest, NOW)
        self.assertEqual(20, len(selected))
        self.assertEqual(30, omitted)
        later, _ = select_targets(latest, NOW + timedelta(hours=4))
        self.assertNotEqual(selected, later)

    def test_missing_or_invalid_upbit_price_is_not_queried(self):
        latest = {"markets": [{"market": "KRW-BTC", "ticker": {"trade_price": 100}},
                              {"market": "KRW-ETH", "ticker": None},
                              {"market": "KRW-SOL", "ticker": {"trade_price": 0}}]}
        self.assertEqual((["KRW-BTC"], 0), select_targets(latest, NOW))

    def test_per_market_api_failure_does_not_remove_valid_observation(self):
        class Client:
            def get_minute_candles(self, market, unit, count):
                if market == "KRW-ETH":
                    raise UpbitAPIError("mock timeout")
                return hours(market=market)

        latest = {"collected_at": {"utc": "2026-09-27T12:00:00Z"},
                  "markets": [{"market": code, "ticker": {"trade_price": 100}}
                              for code in ("KRW-BTC", "KRW-ETH")]}
        result = collect(latest, Client(), now=NOW)
        self.assertEqual(2, result["request_count"])
        self.assertEqual("AVAILABLE", result["markets"]["KRW-BTC"]["status"])
        self.assertEqual("FETCH_FAILED", result["markets"]["KRW-ETH"]["status"])
        self.assertEqual("2026-09-27T12:00:00Z", result["source_latest_collected_at"])


class FourHourTests(unittest.TestCase):
    def candles(self):
        first = NOW.replace(minute=0) - timedelta(hours=84)
        return [{"market": "KRW-BTC", "candle_date_time_utc": (first + timedelta(hours=4*i)).isoformat(),
                 "trade_price": 100+i, "candle_acc_trade_volume": 10,
                 "candle_acc_trade_price": 1000} for i in range(21)]

    def test_complete_direct_four_hour_window_excludes_open_candle(self):
        rows = self.candles()
        rows[-1]["candle_acc_trade_volume"] = 30
        rows.append({**rows[-1], "candle_date_time_utc": "2026-09-27T12:00:00Z", "trade_price": 99999})
        result = calculate_four_hour(rows, NOW, "KRW-BTC")
        self.assertEqual("AVAILABLE", result["status"])
        self.assertEqual(3, result["rvol_4h"])
        self.assertAlmostEqual((120/119-1)*100, result["price_change_pct_4h"], places=5)
        self.assertNotIn("rvol_1h", result)

    def test_missing_or_stale_candles_never_become_zero_filled(self):
        rows = self.candles()
        self.assertEqual("INSUFFICIENT_HISTORY", calculate_four_hour(rows[:-1], NOW, "KRW-BTC")["status"])
        self.assertEqual("STALE", calculate_four_hour(rows, NOW+timedelta(hours=4), "KRW-BTC")["status"])

    def test_every_valid_market_is_queried_not_twenty_market_rotation(self):
        class API:
            def __init__(api): api.calls=[]
            def get_minute_candles(api, code, unit, count):
                api.calls.append((code,unit,count))
                return [{**row, "market": code} for row in self.candles()]
        api=API()
        latest={"collected_at":{"utc":"2026-09-27T12:00:00Z"}, "markets":[
            {"market":f"KRW-C{i}","ticker":{"trade_price":100}} for i in range(31)]}
        result=collect_four_hour(latest,api,now=NOW)
        self.assertEqual(31,len(api.calls))
        self.assertTrue(all(unit==240 for _,unit,_ in api.calls))
        self.assertEqual(0,result["omitted_market_count"])
        self.assertTrue(all(row["status"]=="AVAILABLE" for row in result["markets"].values()))


if __name__ == "__main__":
    unittest.main()
