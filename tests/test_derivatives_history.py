import tempfile
import unittest
from pathlib import Path

from collector.derivatives_history import (HOUR_MS, RETENTION_DAYS,
                                           add_observation_coverage, load_history, update_history)


T = 1_790_000_000_000


def snapshot(at, quantity, symbol="BTCUSDT"):
    return {"markets": {"KRW-BTC": {"bitget": {
        "match": {"status": "VERIFIED", "symbol": symbol},
        "oi": {"at_ms": at, "exchange_quantity": quantity,
               "changes": {f"{hours}h": {"pct": None, "reference_at_ms": None}
                           for hours in (1, 4, 24)}},
        "funding": {"current_rate": 0.01, "current_observed_at_ms": at,
                    "latest_rate": 0.02, "latest_at_ms": at - HOUR_MS,
                    "previous_rate": 0.03, "previous_at_ms": at - 2 * HOUR_MS},
        "long_short": {"general_accounts": {"ratio": 1.2, "at_ms": at},
                       "active_accounts": {"ratio": 1.1, "at_ms": at},
                       "active_positions": {"ratio": 0.9, "at_ms": at}},
    }}}}


class HistoryTests(unittest.TestCase):
    def test_current_oi_does_not_count_as_an_available_change(self):
        document = snapshot(T, 100)
        document["summary"] = {"provider_counts": {"bitget": {"oi_available": 1}}}
        add_observation_coverage(document)
        counts = document["summary"]["provider_counts"]["bitget"]
        self.assertEqual(1, counts["oi_available"])
        self.assertEqual({"1h": 0, "4h": 0, "24h": 0}, counts["oi_change_available"])
        self.assertEqual(1, counts["funding_observed"])

    def test_real_four_and_24_hour_observations_only(self):
        history = {"schema_version": "1.0", "markets": {}}
        first = snapshot(T, 100)
        update_history(history, first, now_ms=T)
        second = snapshot(T + 4 * HOUR_MS + 2 * 60_000, 110)
        update_history(history, second, now_ms=T + 4 * HOUR_MS + 2 * 60_000)
        self.assertEqual(second["markets"]["KRW-BTC"]["bitget"]["oi"]["changes"]["4h"]["pct"], 10.0)
        self.assertIsNone(second["markets"]["KRW-BTC"]["bitget"]["oi"]["changes"]["24h"]["pct"])
        third = snapshot(T + 24 * HOUR_MS + 1 * 60_000, 120)
        update_history(history, third, now_ms=T + 24 * HOUR_MS + 1 * 60_000)
        changes = third["markets"]["KRW-BTC"]["bitget"]["oi"]["changes"]
        self.assertEqual(changes["24h"]["pct"], 20.0)
        self.assertEqual(changes["24h"]["reference_at_ms"], T)
        self.assertIsNone(changes["1h"]["pct"])
        update_history(history, third, now_ms=T + 24 * HOUR_MS + 1 * 60_000)
        self.assertEqual(len(history["markets"]["KRW-BTC"]["oi"]), 3)
        self.assertEqual(len(history["markets"]["KRW-BTC"]["funding_settled"]), 6)

    def test_wrong_interval_and_symbol_change_do_not_reuse_old_oi(self):
        history = {"schema_version": "1.0", "markets": {}}
        update_history(history, snapshot(T, 100), now_ms=T)
        late = snapshot(T + 4 * HOUR_MS + 11 * 60_000, 110)
        update_history(history, late, now_ms=T + 4 * HOUR_MS + 11 * 60_000)
        self.assertIsNone(late["markets"]["KRW-BTC"]["bitget"]["oi"]["changes"]["4h"]["pct"])
        changed = snapshot(T + 8 * HOUR_MS, 200, symbol="1000BTCUSDT")
        update_history(history, changed, now_ms=T + 8 * HOUR_MS)
        self.assertEqual(len(history["markets"]["KRW-BTC"]["oi"]), 1)

    def test_retention_and_invalid_file(self):
        history = {"schema_version": "1.0", "markets": {}}
        update_history(history, snapshot(T, 100), now_ms=T)
        update_history(history, {"markets": {}}, now_ms=T + (RETENTION_DAYS + 1) * 24 * HOUR_MS)
        self.assertEqual(history["markets"], {})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.json"
            self.assertEqual(load_history(path)["markets"], {})
            path.write_text('{"schema_version":"unknown","markets":{}}', encoding="utf-8")
            with self.assertRaises(ValueError):
                load_history(path)


if __name__ == "__main__":
    unittest.main()
