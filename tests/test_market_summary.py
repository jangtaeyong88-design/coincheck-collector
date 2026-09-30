import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from collector.market_summary import FIELDS, build_market_summary
from collector.market_summary_main import main


def indicator_record(market, *, complete=True):
    return {
        "market": market,
        "collection": {"success": True},
        "indicators": {
            "as_of": "2026-09-23T00:00:00",
            "price_change_pct": {"1d": 1, "3d": 3, "7d": 7},
            "rvol": {"1d": 0.4, "3d": 0.8, "7d": 1.2 if complete else None},
            "volume_dry_up": {"is_dry_up": True},
            "atr_14": 4.2,
            "bollinger_band_width_20_pct": 8.5,
            "recent_box": {"duration_days": 9, "width_pct": 6.1},
            "distance_from_20d_low_pct": 12.3,
        },
    }


def source_snapshots():
    latest = {
        "collected_at": {"utc": "2026-09-24T00:00:00Z", "kst": "2026-09-24T09:00:00+09:00"},
        "markets": [
            {"market": "KRW-BTC", "ticker": {"trade_price": 100}, "errors": []},
            {"market": "KRW-ETH", "ticker": None, "errors": [{"stage": "ticker"}]},
        ],
    }
    indicators = {
        "collected_at": {"utc": "2026-09-24T00:01:30Z", "kst": "2026-09-24T09:01:30+09:00"},
        "markets": [indicator_record("KRW-BTC"), indicator_record("KRW-SOL", complete=False)],
    }
    return latest, indicators


class MarketSummaryTests(unittest.TestCase):
    def test_full_union_metrics_counts_and_source_times(self):
        latest, indicators = source_snapshots()
        result = build_market_summary(
            latest, indicators, generated_at=datetime(2026, 9, 24, 0, 2, tzinfo=UTC)
        )
        self.assertEqual(list(result["markets"]), ["KRW-BTC", "KRW-ETH", "KRW-SOL"])
        self.assertEqual(result["fields"], list(FIELDS))
        self.assertEqual(len(FIELDS), len(result["markets"]["KRW-BTC"]))
        btc = dict(zip(FIELDS, result["markets"]["KRW-BTC"]))
        self.assertEqual(btc["price"], 100)
        self.assertEqual([btc[f"price_change_pct_{d}"] for d in ("1d", "3d", "7d")], [1, 3, 7])
        self.assertEqual([btc[f"rvol_{d}"] for d in ("1d", "3d", "7d")], [0.4, 0.8, 1.2])
        self.assertTrue(btc["volume_dry_up"])
        self.assertEqual(btc["atr_14"], 4.2)
        self.assertEqual(btc["bollinger_band_width_20_pct"], 8.5)
        self.assertEqual((btc["box_duration_days"], btc["box_width_pct"]), (9, 6.1))
        self.assertEqual("FOUND", btc["box_status"])
        self.assertEqual("2026-09-24T00:00:00Z", btc["daily_candle_ended_at"])
        self.assertEqual(btc["distance_from_20d_low_pct"], 12.3)
        self.assertTrue(btc["collection_success"])
        self.assertEqual(result["summary"], {
            "market_count": 3,
            "successful_market_count": 1,
            "failed_market_count": 2,
            "insufficient_data_market_count": 1,
        })
        self.assertEqual(result["indicators_minus_latest_seconds"], 90)
        self.assertEqual(result["source_collected_at"]["latest"], latest["collected_at"])
        self.assertEqual(result["source_collected_at"]["indicators"], indicators["collected_at"])
        self.assertEqual(result["generated_at"]["kst"], "2026-09-24T09:02:00+09:00")

    def test_missing_data_stays_null(self):
        latest, indicators = source_snapshots()
        del latest["collected_at"]
        result = build_market_summary(latest, indicators)
        eth = dict(zip(FIELDS, result["markets"]["KRW-ETH"]))
        sol = dict(zip(FIELDS, result["markets"]["KRW-SOL"]))
        self.assertIsNone(eth["price"])
        self.assertIsNone(eth["atr_14"])
        self.assertIsNone(eth["daily_candle_ended_at"])
        self.assertFalse(eth["indicators_available"])
        self.assertEqual("INSUFFICIENT_HISTORY", eth["box_status"])
        self.assertIsNone(sol["price"])
        self.assertFalse(sol["latest_available"])
        self.assertIsNone(sol["rvol_7d"])
        self.assertTrue(sol["insufficient_data"])
        self.assertIsNone(result["source_collected_at"]["latest"]["utc"])
        self.assertIsNone(result["indicators_minus_latest_seconds"])

    def test_completed_history_without_a_narrow_box_is_not_missing_data(self):
        latest, indicators = source_snapshots()
        indicators["markets"][0]["indicators"]["recent_box"] = {
            "duration_days": 0, "width_pct": None}
        row = dict(zip(FIELDS, build_market_summary(latest, indicators)["markets"]["KRW-BTC"]))
        self.assertEqual("NO_BOX", row["box_status"])
        self.assertFalse(row["insufficient_data"])

    def test_cli_writes_compact_atomic_json(self):
        latest, indicators = source_snapshots()
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            latest_path = folder / "latest.json"
            indicators_path = folder / "indicators.json"
            output = folder / "market_summary.json"
            latest_path.write_text(json.dumps(latest), encoding="utf-8")
            indicators_path.write_text(json.dumps(indicators), encoding="utf-8")
            with patch("sys.argv", ["market_summary_main", "--latest", str(latest_path),
                                    "--indicators", str(indicators_path), "--output", str(output)]):
                self.assertEqual(main(), 0)
            content = output.read_text(encoding="utf-8")
            self.assertTrue(content.endswith("\n"))
            self.assertNotIn("\n  ", content)
            self.assertEqual(json.loads(content)["summary"]["market_count"], 3)
            self.assertNotIn("daily_candles", content)


if __name__ == "__main__":
    unittest.main()
