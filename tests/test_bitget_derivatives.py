import unittest

from collector.bitget_derivatives import add_bitget, match_bitget
from collector.derivatives import DerivativesAPIError


def contract(base="BTC", **changes):
    row = {"symbol": f"{base}USDT", "category": "USDT-FUTURES",
           "baseCoin": base, "quoteCoin": "USDT", "type": "perpetual",
           "status": "online", "symbolType": "crypto"}
    row.update(changes)
    return row


def snapshot(codes):
    return {"summary": {}, "catalog_errors": {}, "exchange_diagnostics": {},
            "markets": {f"KRW-{code}": {
                "binance": {"match": {"status": "FETCH_FAILED"}, "errors": []},
                "bybit": {"match": {"status": "FETCH_FAILED"}, "errors": []}}
                for code in codes}}


class FakeClient:
    def __init__(self, fail_catalog=False, fail_path=None, unit_ticker=None):
        self.fail_catalog = fail_catalog
        self.fail_path = fail_path
        self.unit_ticker = unit_ticker
        self.calls = []

    def get(self, path, params=None):
        self.calls.append((path, params))
        if path == self.fail_path:
            raise DerivativesAPIError("HTTP 503")
        if path.endswith("instruments"):
            if self.fail_catalog:
                raise DerivativesAPIError("HTTP 403", details={"http_status": 403,
                                                                "category": "FORBIDDEN_UNDETERMINED"})
            return {"data": [contract()]}
        if path.endswith("/market/ticker"):
            return {"data": [self.unit_ticker] if self.unit_ticker else []}
        if path.endswith("open-interest"):
            return {"data": {"list": [{"symbol": "BTCUSDT", "openInterest": "100.5"}],
                             "ts": "1790223546427"}}
        if path.endswith("current-fund-rate"):
            return {"requestTime": "1790223547000", "data": [{"symbol": "BTCUSDT", "fundingRate": "0.001",
                              "nextUpdate": "1790236800000"}]}
        if path.endswith("history-fund-rate"):
            return {"data": {"resultList": [
                {"fundingRate": "0.003", "fundingRateTimestamp": "1790208000000"},
                {"fundingRate": "0.002", "fundingRateTimestamp": "1790179200000"}]}}
        if path.endswith("futures-long-short"):
            return {"data": [{"longShortRatio": "1.5", "ts": "1000"},
                             {"longShortRatio": "2.0", "ts": "2000"}]}
        if path.endswith("futures-account-long-short"):
            return {"data": [{"longShortAccountRatio": "1.2", "ts": "2000"}]}
        if path.endswith("futures-position-long-short"):
            return {"data": [{"longShortPositionRatio": "0.8", "ts": "2000"}]}
        if path.endswith("liquidations"):
            return {"data": {"list": [
                {"symbol": "BTCUSDT", "side": "buy", "ts": "2000"},
                {"symbol": "BTCUSDT", "side": "sell", "ts": "1000"}],
                "cursor": "older"}}
        raise AssertionError(path)


class BitgetTests(unittest.TestCase):
    def test_matching_does_not_infer_identity_or_multiplier(self):
        self.assertEqual(match_bitget("KRW-BTC", {"english_name": "Bitcoin"},
                                      [contract()])["status"], "VERIFIED")
        self.assertEqual(match_bitget("KRW-BTC", {"english_name": "Other"},
                                      [contract()])["status"], "UNVERIFIED")
        self.assertEqual(match_bitget("KRW-BTC", {"english_name": "Bitcoin"},
                                      [contract("1000BTC")])["status"], "UNVERIFIED")
        self.assertEqual(match_bitget("KRW-ETH", {"english_name": "Ethereum"},
                                      [contract()])["status"], "NO_MARKET")
        shib = match_bitget("KRW-SHIB", {"english_name": "Shiba Inu"},
                           [contract("1000SHIB")])
        self.assertEqual(shib["base_units_per_exchange_unit"], 1000)

    def test_collects_only_verified_fields_and_leaves_unknowns_null(self):
        latest = {"markets": [
            {"market": "KRW-BTC", "metadata": {"english_name": "Bitcoin"}},
            {"market": "KRW-ETH", "metadata": {"english_name": "Ethereum"}}]}
        client = FakeClient()
        result = add_bitget(snapshot(("BTC", "ETH")), latest, client,
                            sleeper=lambda _: None, bitget_coins=[], references=[])
        btc = result["markets"]["KRW-BTC"]["bitget"]
        self.assertEqual(btc["match"]["status"], "VERIFIED")
        self.assertEqual(btc["oi"]["exchange_quantity"], 100.5)
        self.assertEqual(btc["oi"]["exchange_quantity_unit"], "UNCONFIRMED")
        self.assertIsNone(btc["oi"]["changes"]["1h"]["pct"])
        self.assertEqual(btc["oi"]["changes"]["1h"]["status"], "INSUFFICIENT_HISTORY")
        self.assertEqual(btc["funding"]["latest_rate"], 0.003)
        self.assertEqual(btc["funding"]["current_rate"], 0.001)
        self.assertEqual(btc["long_short"]["general_accounts"]["ratio"], 2.0)
        self.assertEqual(btc["long_short"]["top_accounts"]["status"], "NOT_SUPPORTED")
        self.assertEqual(btc["long_short"]["active_positions"]["ratio"], 0.8)
        self.assertEqual(btc["long_short"]["active_accounts"]["ratio"], 1.2)
        self.assertEqual(btc["liquidations"]["sampled_buy_count"], 1)
        self.assertIsNone(btc["liquidations"]["24h"]["long"])
        self.assertEqual(result["markets"]["KRW-ETH"]["bitget"]["match"]["status"],
                         "NO_MARKET")
        self.assertEqual(result["summary"]["matched_market_count"], 1)
        self.assertEqual(result["summary"]["provider_counts"]["bitget"]["oi_available"], 1)

    def test_catalog_failure_is_not_market_absence(self):
        latest = {"markets": [{"market": "KRW-BTC",
                               "metadata": {"english_name": "Bitcoin"}}]}
        result = add_bitget(snapshot(("BTC",)), latest, FakeClient(True),
                            sleeper=lambda _: None, bitget_coins=[], references=[])
        self.assertEqual(result["markets"]["KRW-BTC"]["bitget"]["match"]["status"],
                         "FETCH_FAILED")
        self.assertEqual(result["exchange_diagnostics"]["bitget"]["http_status"], 403)
        self.assertIsNone(result["markets"]["KRW-BTC"]["bitget"]["oi"])

    def test_single_endpoint_failure_preserves_other_values(self):
        latest = {"markets": [{"market": "KRW-BTC",
                               "metadata": {"english_name": "Bitcoin"}}]}
        result = add_bitget(snapshot(("BTC",)), latest,
                            FakeClient(fail_path="/api/v3/market/open-interest"),
                            sleeper=lambda _: None, bitget_coins=[], references=[])
        btc = result["markets"]["KRW-BTC"]["bitget"]
        self.assertEqual(btc["oi"]["status"], "FETCH_FAILED")
        self.assertIsNone(btc["oi"]["exchange_quantity"])
        self.assertEqual(btc["funding"]["current_status"], "AVAILABLE")
        self.assertEqual(btc["long_short"]["general_accounts"]["status"], "AVAILABLE")
        self.assertEqual(btc["errors"][0]["stage"], "oi_current")

    def test_oi_unit_and_usdt_value_require_official_v2_crosscheck(self):
        latest = {"markets": [{"market": "KRW-BTC",
                               "metadata": {"english_name": "Bitcoin"}}]}
        ticker = {"symbol": "BTCUSDT", "holdingAmount": "100.4",
                  "markPrice": "50000", "ts": "1790223546428"}
        result = add_bitget(snapshot(("BTC",)), latest, FakeClient(unit_ticker=ticker),
                            sleeper=lambda _: None, bitget_coins=[], references=[])
        oi = result["markets"]["KRW-BTC"]["bitget"]["oi"]
        self.assertEqual(oi["exchange_quantity_unit"], "BTC")
        self.assertEqual(oi["base_asset_quantity"], 100.5)
        self.assertEqual(oi["value_usdt"], 5_025_000)
        ticker["holdingAmount"] = "200"
        result = add_bitget(snapshot(("BTC",)), latest, FakeClient(unit_ticker=ticker),
                            sleeper=lambda _: None, bitget_coins=[], references=[])
        oi = result["markets"]["KRW-BTC"]["bitget"]["oi"]
        self.assertEqual(oi["exchange_quantity_unit"], "UNCONFIRMED")
        self.assertIsNone(oi["value_usdt"])


if __name__ == "__main__":
    unittest.main()
