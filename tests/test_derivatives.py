import json
import tempfile
import unittest
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from collector.derivatives import (
    HOURS_MS, DerivativesAPIError, PublicClient, _catalog, _oi_changes,
    build_derivatives_snapshot,
    match_contract,
)
from collector.derivatives_main import main
from collector.derivatives_diagnostic import probe


def upbit(*codes):
    names = {"BTC": "Bitcoin", "ETH": "Ethereum", "SHIB": "Shiba Inu",
             "ABC": "Unknown Asset", "MISS": "Missing Coin"}
    return {"markets": [{"market": f"KRW-{code}",
                         "metadata": {"english_name": names[code]}} for code in codes],
            "collected_at": {"utc": "2026-09-24T00:00:00Z"}}


def binance_instrument(base="BTC", **changes):
    row = {"symbol": f"{base}USDT", "baseAsset": base, "quoteAsset": "USDT",
           "marginAsset": "USDT", "underlyingType": "COIN",
           "contractType": "PERPETUAL", "status": "TRADING"}
    row.update(changes)
    return row


def bybit_instrument(base="BTC", **changes):
    row = {"symbol": f"{base}USDT", "baseCoin": base, "quoteCoin": "USDT",
           "settleCoin": "USDT", "contractType": "LinearPerpetual",
           "status": "Trading", "isPreListing": False}
    row.update(changes)
    return row


class FakeClient:
    def __init__(self, exchange, instruments, *, fail=None, ticker=True):
        self.exchange = exchange
        self.instruments = instruments
        self.fail = set(fail or ())
        self.ticker = ticker
        self.calls = []

    def get(self, path, params=None, headers=None):
        self.calls.append((path, params, headers))
        if path in self.fail:
            raise DerivativesAPIError("mock API failure")
        symbol = (params or {}).get("symbol", "BTCUSDT")
        if self.exchange == "binance":
            if path.endswith("exchangeInfo"):
                return {"symbols": self.instruments}
            if path.endswith("/openInterest"):
                return {"symbol": symbol, "openInterest": "100", "time": 100 * HOURS_MS}
            if path.endswith("openInterestHist"):
                return [{"sumOpenInterest": str(value), "sumOpenInterestValue": "5000",
                         "timestamp": t * HOURS_MS}
                        for t, value in ((99, 90), (96, 70), (76, 50))]
            if path.endswith("fundingRate"):
                return [{"fundingRate": "0.001", "fundingTime": 98 * HOURS_MS},
                        {"fundingRate": "0.002", "fundingTime": 100 * HOURS_MS}]
            if "LongShort" in path:
                return [{"longShortRatio": "1.5", "timestamp": 99 * HOURS_MS}]
        else:
            if path.endswith("instruments-info"):
                return {"retCode": 0, "result": {"list": self.instruments, "nextPageCursor": ""}}
            if path.endswith("tickers"):
                return {"retCode": 0, "time": 100 * HOURS_MS, "result": {"list": [
                    {"symbol": row["symbol"], "openInterest": "100",
                     "openInterestValue": "6000"} for row in self.instruments] if self.ticker else []}}
            if path.endswith("open-interest"):
                return {"retCode": 0, "result": {"list": [
                    {"openInterest": str(value), "timestamp": str(t * HOURS_MS)}
                    for t, value in ((99, 90), (96, 70), (76, 50))]}}
            if path.endswith("funding/history"):
                return {"retCode": 0, "result": {"list": [
                    {"fundingRate": "0.003", "fundingRateTimestamp": str(100 * HOURS_MS)},
                    {"fundingRate": "0.001", "fundingRateTimestamp": str(92 * HOURS_MS)}]}}
            if path.endswith("account-ratio"):
                return {"retCode": 0, "result": {"list": [
                    {"buyRatio": "0.6", "sellRatio": "0.4",
                     "timestamp": str(99 * HOURS_MS)}]}}
        raise AssertionError(path)


class MatchingTests(unittest.TestCase):
    def test_requires_identity_and_contract_metadata(self):
        self.assertEqual(match_contract("KRW-BTC", {"english_name": "Bitcoin"},
                                        [binance_instrument()], "binance")["status"], "VERIFIED")
        self.assertEqual(match_contract("KRW-BTC", {"english_name": "Other Bitcoin"},
                                        [binance_instrument()], "binance")["status"], "UNVERIFIED")
        self.assertEqual(match_contract("KRW-ABC", {"english_name": "Unknown Asset"},
                                        [binance_instrument("ABC")], "binance")["status"], "UNVERIFIED")
        for changed in ({"baseAsset": "ETH"}, {"status": "BREAK"},
                        {"contractType": "CURRENT_QUARTER"}, {"quoteAsset": "USDC"}):
            with self.subTest(changed=changed):
                self.assertEqual(match_contract("KRW-BTC", {"english_name": "Bitcoin"},
                                                [binance_instrument(**changed)], "binance")["status"],
                                 "UNVERIFIED")

    def test_scaled_contract_and_ambiguous_contracts(self):
        match = match_contract("KRW-SHIB", {"english_name": "Shiba Inu"},
                               [bybit_instrument("1000SHIB")], "bybit")
        self.assertEqual((match["status"], match["base_units_per_exchange_unit"]),
                         ("VERIFIED", 1000))
        self.assertEqual(match_contract("KRW-ABC", {"english_name": "Unknown Asset"},
                                        [bybit_instrument("1000ABC")], "bybit")["status"],
                         "UNVERIFIED")
        self.assertEqual(match_contract("KRW-SHIB", {"english_name": "Shiba Inu"},
                                        [bybit_instrument("SHIB"), bybit_instrument("1000SHIB")],
                                        "bybit")["status"], "UNVERIFIED")
        self.assertEqual(match_contract("KRW-BTC", {"english_name": "Bitcoin"}, [],
                                        "binance")["status"], "ABSENT")


class CollectionTests(unittest.TestCase):
    def test_catalog_failure_keeps_structured_exchange_cause(self):
        class DeniedClient:
            def get(self, path, params=None, headers=None):
                raise DerivativesAPIError("fapi.binance.com: HTTP 451",
                    details={"host": "fapi.binance.com", "path": path,
                             "http_status": 451, "category": "LEGAL_RESTRICTION"})
        result = build_derivatives_snapshot(upbit("BTC"), DeniedClient(),
                                            FakeClient("bybit", []))
        self.assertEqual(result["catalog_errors"]["binance"],
                         "fapi.binance.com: HTTP 451")
        self.assertEqual(result["exchange_diagnostics"]["binance"]["http_status"], 451)
        self.assertEqual(result["markets"]["KRW-BTC"]["binance"]["match"]["status"],
                         "FETCH_FAILED")
        self.assertIsNone(result["markets"]["KRW-BTC"]["binance"]["oi"])

    def test_btc_eth_sol_probe_marks_actual_values_separately(self):
        source = {"markets": [{"market": f"KRW-{code}",
                               "metadata": {"english_name": name}}
                              for code, name in (("BTC", "Bitcoin"), ("ETH", "Ethereum"),
                                                 ("SOL", "Solana"))]}
        clients = (FakeClient("binance", [binance_instrument(code)
                                          for code in ("BTC", "ETH", "SOL")]),
                   FakeClient("bybit", [bybit_instrument(code)
                                        for code in ("BTC", "ETH", "SOL")]))
        with patch("collector.derivatives_diagnostic.default_clients", return_value=clients):
            events, success = probe(source, check_alternate=False)
        self.assertTrue(success)
        values = [event for event in events if event["event"] == "market_probe"]
        self.assertEqual(len(values), 6)
        self.assertTrue(all(event["status"] == "SUCCESS"
                            and event["oi_quantity"] == 100
                            and event["funding_rate"] is not None for event in values))

    def test_probe_reports_partial_when_funding_fails(self):
        source = upbit("BTC")
        clients = (FakeClient("binance", [binance_instrument()],
                              fail={"/fapi/v1/fundingRate"}),
                   FakeClient("bybit", [bybit_instrument()]))
        with patch("collector.derivatives_diagnostic.default_clients", return_value=clients):
            events, success = probe(source, check_alternate=False)
        self.assertFalse(success)
        btc = next(event for event in events if event.get("market") == "KRW-BTC"
                   and event.get("exchange") == "binance")
        self.assertEqual(btc["status"], "PARTIAL")
        self.assertEqual(btc["oi_status"], "AVAILABLE")
        self.assertEqual(btc["funding_status"], "UNAVAILABLE")

    def test_cli_writes_compact_snapshot_without_replacing_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "latest.json"
            output = Path(directory) / "derivatives_summary.json"
            history = Path(directory) / "derivatives_history.json"
            source.write_text(json.dumps(upbit("BTC")), encoding="utf-8")
            clients = (FakeClient("binance", [binance_instrument()]),
                       FakeClient("bybit", [bybit_instrument()]))
            with patch("collector.derivatives_main.default_clients", return_value=clients), \
                 patch("collector.derivatives_main.add_bitget", side_effect=lambda result, *_: result), \
                 patch("sys.argv", ["derivatives_main", "--latest", str(source),
                                    "--output", str(output), "--history", str(history),
                                    "--identity-cache", str(Path(directory) / "identity_cache.json"),
                                    "--identity-baseline", str(Path(directory) / "identity_baseline.json"),
                                    "--optional-provider-test", str(Path(directory) / "provider_test.json"),
                                    "--market-summary", str(Path(directory) / "market_summary.json")]):
                self.assertEqual(main(), 0)
            self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["summary"]["market_count"], 1)
            self.assertNotIn("\n  ", output.read_text(encoding="utf-8"))
            self.assertEqual(json.loads(source.read_text(encoding="utf-8")), upbit("BTC"))
            self.assertEqual(json.loads(history.read_text(encoding="utf-8"))["schema_version"], "1.0")

    def test_bybit_catalog_uses_all_pages(self):
        class PagedClient:
            def get(self, path, params=None):
                if params.get("cursor") == "page-two":
                    return {"result": {"list": [bybit_instrument("ETH")],
                                       "nextPageCursor": ""}}
                return {"result": {"list": [bybit_instrument("BTC")],
                                   "nextPageCursor": "page-two"}}
        self.assertEqual(len(_catalog(PagedClient(), "bybit")), 2)

    def test_missing_oi_reference_remains_null(self):
        changes = _oi_changes(100, 100 * HOURS_MS,
                              [{"timestamp": 70 * HOURS_MS, "openInterest": "20"}],
                              "openInterest")
        self.assertTrue(all(item["pct"] is None for item in changes.values()))

    def test_oi_funding_ratios_and_unsupported_liquidations(self):
        result = build_derivatives_snapshot(
            upbit("BTC", "SHIB", "MISS"),
            FakeClient("binance", [binance_instrument()]),
            FakeClient("bybit", [bybit_instrument(), bybit_instrument("1000SHIB")]),
            now=datetime(2026, 9, 24, tzinfo=UTC),
        )
        self.assertEqual(result["summary"]["market_count"], 3)
        self.assertEqual(result["summary"]["matched_market_count"], 2)
        self.assertEqual(result["summary"]["absent_market_count"], 1)
        self.assertEqual(result["markets"]["KRW-MISS"]["binance"]["match"]["status"], "ABSENT")
        binance = result["markets"]["KRW-BTC"]["binance"]
        self.assertEqual(binance["oi"]["exchange_quantity"], 100)
        self.assertIsNone(binance["oi"]["contract_quantity"])
        self.assertIsNone(binance["oi"]["base_asset_quantity"])
        self.assertEqual(binance["oi"]["value_usdt"], 5000)
        self.assertAlmostEqual(binance["oi"]["changes"]["1h"]["pct"], (100 / 90 - 1) * 100)
        self.assertEqual(binance["oi"]["changes"]["4h"]["reference_at_ms"], 96 * HOURS_MS)
        self.assertEqual(binance["oi"]["changes"]["24h"]["pct"], 100)
        self.assertEqual(binance["funding"]["direction"], "UP")
        self.assertEqual(binance["long_short"]["general_accounts"]["ratio"], 1.5)
        self.assertEqual(binance["long_short"]["top_accounts"]["status"], "NOT_SUPPORTED")
        self.assertEqual(binance["liquidations"]["status"], "INSUFFICIENT_HISTORY")
        self.assertIsNone(binance["liquidations"]["24h"]["long"])
        shib = result["markets"]["KRW-SHIB"]["bybit"]
        self.assertEqual(shib["oi"]["exchange_quantity_unit"], "1000SHIB")
        self.assertEqual(shib["oi"]["base_asset_quantity"], 100_000)
        self.assertEqual(shib["oi"]["value_usdt"], 6000)
        self.assertEqual(shib["long_short"]["general_accounts"]["ratio"], 1.5)
        self.assertEqual(shib["long_short"]["top_positions"]["status"], "NOT_SUPPORTED")
        self.assertLess(len(json.dumps(result, separators=(",", ":"))), 7000)

    def test_missing_history_api_failure_and_unverified_do_not_fabricate_data(self):
        binance = FakeClient("binance", [binance_instrument(), binance_instrument("ABC")],
                             fail={"/futures/data/openInterestHist"})
        bybit = FakeClient("bybit", [], fail={"/v5/market/instruments-info"})
        result = build_derivatives_snapshot(upbit("BTC", "ABC"), binance, bybit)
        btc = result["markets"]["KRW-BTC"]["binance"]
        self.assertIsNone(btc["oi"]["changes"]["1h"]["pct"])
        self.assertEqual(btc["errors"][0]["stage"], "oi_history")
        self.assertEqual(result["summary"]["failed_market_count"], 2)
        self.assertEqual(result["markets"]["KRW-ABC"]["binance"]["match"]["status"], "UNVERIFIED")
        self.assertIsNone(result["markets"]["KRW-ABC"]["binance"]["oi"])
        self.assertEqual(result["markets"]["KRW-BTC"]["bybit"]["match"]["status"], "FETCH_FAILED")
        self.assertFalse(any("openInterest" in call[0] for call in binance.calls
                             if call[1] and call[1].get("symbol") == "ABCUSDT"))

    def test_missing_reference_and_binance_key_gate(self):
        binance = FakeClient("binance", [binance_instrument()])
        bybit = FakeClient("bybit", [])
        result = build_derivatives_snapshot(upbit("BTC"), binance, bybit, api_key="test-key")
        ratios = result["markets"]["KRW-BTC"]["binance"]["long_short"]
        self.assertEqual(ratios["top_accounts"]["status"], "AVAILABLE")
        self.assertEqual(ratios["top_positions"]["status"], "AVAILABLE")
        self.assertEqual(sum("topLongShort" in call[0] for call in binance.calls), 2)

    def test_long_short_fetch_failure_is_not_labeled_unsupported(self):
        binance = FakeClient("binance", [binance_instrument()],
                             fail={"/futures/data/topLongShortAccountRatio"})
        result = build_derivatives_snapshot(upbit("BTC"), binance,
                                            FakeClient("bybit", []), api_key="test-key")
        record = result["markets"]["KRW-BTC"]["binance"]
        self.assertEqual(record["long_short"]["top_accounts"]["status"], "FETCH_FAILED")
        self.assertEqual(record["long_short"]["top_positions"]["status"], "AVAILABLE")
        self.assertEqual(result["summary"]["failed_market_count"], 1)


class HTTPTests(unittest.TestCase):
    def test_legal_and_ambiguous_forbidden_fail_without_retry(self):
        for status, body, expected in (
            (451, b'{"code":0,"msg":"Unavailable for legal reasons"}', "LEGAL_RESTRICTION"),
            (403, b'<html>Forbidden</html>', "FORBIDDEN_UNDETERMINED"),
            (403, b'{ error:The Amazon CloudFront distribution is configured to block access from your country }', "REGION_RESTRICTION"),
            (403, b'{"retMsg":"access too frequent"}', "RATE_LIMIT"),
        ):
            with self.subTest(status=status, body=body):
                class Opener:
                    calls = 0
                    def open(self, request, timeout):
                        self.calls += 1
                        raise HTTPError(request.full_url, status, "denied",
                                        {"Retry-After": "60"}, BytesIO(body))
                opener = Opener()
                client = PublicClient("https://api.bybit.com", opener=opener,
                                      sleeper=lambda _: None)
                with self.assertRaises(DerivativesAPIError) as caught:
                    client.get("/v5/market/instruments-info")
                self.assertEqual(caught.exception.details["category"], expected)
                self.assertEqual(caught.exception.details["host"], "api.bybit.com")
                self.assertEqual(caught.exception.details["retry_after"], "60")
                self.assertEqual(opener.calls, 1)

    def test_retries_rate_limit_then_decodes_public_response(self):
        class Response:
            def __enter__(self):
                return self
            def __exit__(self, *_):
                pass
            def read(self):
                return b'{"retCode":0,"result":{"list":[]}}'
        class Opener:
            calls = 0
            def open(self, request, timeout):
                self.calls += 1
                if self.calls == 1:
                    raise HTTPError(request.full_url, 429, "rate limit", {}, BytesIO())
                return Response()
        opener = Opener()
        client = PublicClient("https://example.test", opener=opener, interval=0,
                              sleeper=lambda _: None)
        self.assertEqual(client.get("/v5/test")["retCode"], 0)
        self.assertEqual(opener.calls, 2)


if __name__ == "__main__":
    unittest.main()
