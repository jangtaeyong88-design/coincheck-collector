import time
import unittest
from datetime import UTC, datetime

from collector.derivatives_history import update_optional_history
from collector.optional_derivatives import (_gate_data, _kucoin_data, _match_gate,
                                            _match_kucoin, _hourly_changes, _verified_identity,
                                            add_optional_providers)
from collector.optional_provider_probe import check


class StubClient:
    def __init__(self, mapping):
        self.mapping = mapping
        self.calls = []
        self.request_count = 0
        self.http_attempt_count = 0
        self.request_duration_seconds = 0.0

    def get(self, path, params=None):
        self.calls.append((path, params))
        self.request_count += 1
        self.http_attempt_count += 1
        return self.mapping[path]


class OptionalTests(unittest.TestCase):
    def test_probe_rejects_stale_or_incomplete_http_200(self):
        now = int(time.time() * 1000)
        evidence = {"http_status": 200, "api_success": True, "status": "AVAILABLE",
                    "error": None, "observed_at_ms": now}
        stale = check({}, evidence.copy(), [{"openInterest": "100", "ts": now - 3 * 3600000}],
                      ("openInterest",), "ts", 7200)
        self.assertEqual(stale["status"], "FETCH_FAILED")
        missing = check({}, evidence.copy(), [{"ts": now}], ("openInterest",), "ts", 7200)
        self.assertEqual(missing["status"], "FETCH_FAILED")
    def test_gate_hourly_history_requires_exact_observation(self):
        at = 1_800_000_000
        rows = [{"time": at - h * 3600, "open_interest": 100 - h} for h in range(25)]
        changes, source = _hourly_changes(rows, "open_interest", "time", seconds=True)
        self.assertEqual(source, at * 1000)
        self.assertEqual(changes["4h"]["reference_at_ms"], (at - 4 * 3600) * 1000)
        self.assertEqual(changes["24h"]["status"], "AVAILABLE")
        rows = [row for row in rows if row["time"] != at - 4 * 3600]
        changes, _ = _hourly_changes(rows, "open_interest", "time", seconds=True)
        self.assertIsNone(changes["4h"]["pct"])

    def test_symbol_collision_or_contract_unit_is_unverified(self):
        metadata = {"english_name": "Bitcoin"}
        gate = {"name": "1000BTC_USDT", "type": "direct", "status": "trading",
                "quanto_multiplier": "1"}
        self.assertEqual(_match_gate("KRW-BTC", metadata, gate, None, None, None)["status"], "UNVERIFIED")
        kucoin = {"symbol": "XBTUSDTM", "baseCurrency": "XBT", "quoteCurrency": "USDT",
                  "settleCurrency": "USDT", "status": "Open", "expireDate": None,
                  "multiplier": 0}
        self.assertEqual(_match_kucoin("KRW-BTC", metadata, kucoin, None, None, None)["status"], "UNVERIFIED")
        self.assertEqual(_match_gate("KRW-BTC", metadata, None, None, None, None)["status"], "ABSENT")

    def test_bitget_identity_conflicting_contract_address_stays_unverified(self):
        identity = _verified_identity("KRW-AAA", {"english_name": "Alpha"},
                                      {"name": "Alpha", "chains": [{"addr": "0x" + "2" * 40}]},
                                      [], "gate", {"status": "VERIFIED", "identity": {
                                          "contract_address": "0x" + "1" * 40}})
        self.assertIsNone(identity)

    def test_gate_preserves_contract_units_and_independent_liquidation(self):
        now = int(time.time() // 3600 * 3600)
        rows = [{"time": now - i * 3600, "open_interest": str(1000 - i * 10),
                 "open_interest_usd": 100000, "mark_price": 100,
                 "lsr_account": 1.1, "top_lsr_account": 1.2, "top_lsr_size": 1.3,
                 "long_liq_usd_new": 2, "short_liq_usd_new": 3} for i in range(25)]
        client = StubClient({"/futures/usdt/contract_stats": rows,
                             "/futures/usdt/funding_rate": [{"t": now - 3600, "r": "0.001"}]})
        data = _gate_data(client, "BTC_USDT", {"quanto_multiplier": "0.001", "funding_rate": "0.002",
                                                 "position_size": "1000", "mark_price": "100"})
        self.assertEqual(data["oi"]["contract_quantity"], 1000)
        self.assertEqual(data["oi"]["base_asset_quantity"], 1)
        self.assertEqual(data["liquidations"]["24h"]["long"], 48)
        self.assertEqual(data["long_short"]["top_accounts"]["ratio"], 1.2)

    def test_gate_stats_failure_does_not_erase_catalog_oi(self):
        client = StubClient({"/futures/usdt/contract_stats": None,
                             "/futures/usdt/funding_rate": []})
        data = _gate_data(client, "BTC_USDT", {"quanto_multiplier": "0.001",
                                                 "position_size": "1000", "mark_price": "100"})
        self.assertEqual(data["oi"]["exchange_quantity"], 1000)
        self.assertEqual(data["oi"]["history_status"], "FETCH_FAILED")
        self.assertIsNone(data["oi"]["changes"]["4h"]["pct"])
        self.assertEqual(data["long_short"]["top_accounts"]["status"], "FETCH_FAILED")

    def test_kucoin_oi_crosscheck_and_unsupported_fields(self):
        now = int(time.time() * 1000)
        current = StubClient({"/api/ua/v2/market/open-interest": {
            "data": [{"symbol": "XBTUSDTM", "openInterest": "1000", "ts": now}]}})
        # A stub that distinguishes current from historical calls.
        class Uta:
            def get(self, path, params=None):
                if "interval" in params:
                    return {"data": [{"openInterest": "1000", "ts": now - now % 3600000 - i * 3600000}
                                     for i in range(25)]}
                return current.get(path, params)
        futures = StubClient({"/api/v1/funding-rate/XBTUSDTM/current": {
            "data": {"value": 0.001, "timePoint": now - 3600000, "fundingTime": now + 3600000}},
                              "/api/v1/contract/funding-rates": {"data": [
                                  {"fundingRate": 0.001, "timepoint": now - 3600000},
                                  {"fundingRate": 0.0005, "timepoint": now - 9 * 3600000}]}})
        data = _kucoin_data(futures, Uta(), "XBTUSDTM",
                            {"symbol": "XBTUSDTM", "baseCurrency": "XBT", "quoteCurrency": "USDT",
                             "settleCurrency": "USDT", "status": "Open", "expireDate": None,
                             "isInverse": False, "openInterest": "1000", "multiplier": 0.001,
                             "markPrice": 100})
        self.assertEqual(data["oi"]["base_asset_quantity"], 1)
        self.assertEqual(data["oi"]["value_usdt"], 100)
        self.assertEqual(data["oi"]["exchange_quantity_unit"], "CONTRACTS")
        self.assertEqual(data["oi"]["unit_verification"], "KUCOIN_OFFICIAL_CONTRACT_MULTIPLIER")
        self.assertEqual(data["oi"]["unit_diagnostics"]["catalog_crosscheck_status"], "MATCH")
        self.assertEqual(data["oi"]["changes"]["24h"]["pct"], 0)
        self.assertEqual(data["funding"]["direction"], "UP")
        self.assertEqual(data["liquidations"]["status"], "NOT_SUPPORTED")
        self.assertEqual(data["long_short"]["top_accounts"]["status"], "NOT_SUPPORTED")

    def test_kucoin_arx_unit_stays_confirmed_when_catalog_oi_is_stale(self):
        now = int(time.time() * 1000)
        catalog_at = now - 10_000
        uta_current = {"data": [{"symbol": "ARXUSDTM", "openInterest": "353520", "ts": now - 500}]}

        class Uta:
            def get(self, path, params=None):
                if "interval" in params:
                    return {"data": [{"openInterest": "353520", "ts": now - now % 3600000 - i * 3600000}
                                     for i in range(25)]}
                return uta_current

        futures = StubClient({"/api/v1/funding-rate/ARXUSDTM/current": {"data": {"value": 0.001}},
                              "/api/v1/contract/funding-rates": {"data": []}})
        contract = {"symbol": "ARXUSDTM", "baseCurrency": "ARX", "quoteCurrency": "USDT",
                    "settleCurrency": "USDT", "status": "Open", "expireDate": None,
                    "isInverse": False, "openInterest": "332314", "multiplier": "10",
                    "markPrice": "0.25"}
        data = _kucoin_data(futures, Uta(), "ARXUSDTM", contract, catalog_at)
        oi = data["oi"]
        self.assertEqual(oi["exchange_quantity_unit"], "CONTRACTS")
        self.assertEqual(oi["contract_quantity"], 353520)
        self.assertEqual(oi["base_asset_quantity"], 3535200)
        self.assertEqual(oi["value_usdt"], 883800)
        self.assertEqual(oi["unit_diagnostics"]["catalog_crosscheck_status"], "MISMATCH")
        self.assertGreater(oi["unit_diagnostics"]["difference_pct"], 5)
        self.assertEqual(oi["unit_diagnostics"]["multiplier"], 10)
        self.assertEqual(oi["unit_diagnostics"]["multiplier_observed_at_ms"], catalog_at)
        self.assertEqual(oi["unit_diagnostics"]["catalog_oi"]["observed_at_ms"], catalog_at)
        self.assertEqual(oi["unit_diagnostics"]["current_oi"]["value_at_ms"], now - 500)

    def test_kucoin_missing_multiplier_keeps_conversion_unconfirmed(self):
        now = int(time.time() * 1000)
        class Uta:
            def get(self, path, params=None):
                if "interval" in params:
                    return {"data": []}
                return {"data": [{"symbol": "ARXUSDTM", "openInterest": "353520", "ts": now}]}
        data = _kucoin_data(StubClient({}), Uta(), "ARXUSDTM",
                            {"symbol": "ARXUSDTM", "baseCurrency": "ARX", "quoteCurrency": "USDT",
                             "settleCurrency": "USDT", "status": "Open", "expireDate": None,
                             "isInverse": False, "openInterest": "353520", "markPrice": "0.25"}, now)
        oi = data["oi"]
        self.assertEqual(oi["exchange_quantity"], 353520)
        self.assertEqual(oi["exchange_quantity_unit"], "UNCONFIRMED")
        self.assertIsNone(oi["contract_quantity"])
        self.assertIsNone(oi["base_asset_quantity"])
        self.assertIsNone(oi["value_usdt"])
        self.assertIn("MULTIPLIER_MISSING_OR_INVALID", oi["unit_diagnostics"]["reason"])

    def test_optional_history_keeps_providers_separate_and_deduplicates(self):
        now = int(time.time() * 1000)
        history = {"schema_version": "1.0", "markets": {}, "summary": {}}
        def data(qty):
            return {"match": {"status": "VERIFIED", "symbol": "BTC_USDT"},
                    "oi": {"at_ms": now, "exchange_quantity": qty},
                    "funding": {}, "long_short": {}}
        snapshot = {"markets": {"KRW-BTC": {"gate": data(1), "kucoin": data(2)}}}
        update_optional_history(history, snapshot, now_ms=now)
        update_optional_history(history, snapshot, now_ms=now)
        self.assertEqual(history["providers"]["gate"]["KRW-BTC"]["oi"], [[now, 1.0]])
        self.assertEqual(history["providers"]["kucoin"]["KRW-BTC"]["oi"], [[now, 2.0]])

    def test_unverified_match_never_fetches_supply_data(self):
        gate = StubClient({})
        kucoin = StubClient({})
        uta = StubClient({})
        snapshot = {"markets": {"KRW-AAA": {"bitget": {
            "match": {"status": "NO_MARKET"}, "oi": None, "funding": None,
            "long_short": None, "liquidations": None, "errors": []}}},
            "summary": {"provider_counts": {}}, "exchange_diagnostics": {}}
        latest = {"markets": [{"market": "KRW-AAA", "metadata": {"english_name": "Alpha"}}]}
        events = []
        add_optional_providers(snapshot, latest, None, gate=gate, kucoin=kucoin, uta=uta,
                               gate_catalog=[{"name": "AAA_USDT", "type": "direct", "status": "trading",
                                              "quanto_multiplier": "1"}],
                               kucoin_catalog=[], gate_currencies=[{"currency": "AAA", "name": "Other"}],
                               kucoin_currencies=[], references=[],
                               on_provider_start=lambda name: events.append((name, "start")),
                               on_provider_finish=lambda name, _: events.append((name, "finish")))
        self.assertEqual(snapshot["markets"]["KRW-AAA"]["gate"]["match"]["status"], "UNVERIFIED")
        self.assertEqual(snapshot["markets"]["KRW-AAA"]["kucoin"]["match"]["status"], "ABSENT")
        self.assertEqual(gate.calls + kucoin.calls + uta.calls, [])
        self.assertEqual(snapshot["summary"]["matched_market_count"], 0)
        self.assertEqual(events, [("gate", "start"), ("gate", "finish"),
                                  ("kucoin", "start"), ("kucoin", "finish")])


if __name__ == "__main__":
    unittest.main()
