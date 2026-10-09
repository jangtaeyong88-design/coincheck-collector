import unittest
from datetime import UTC, datetime
from collector.market_turnover import add_turnover
from collector.derivatives import build_derivatives_snapshot


class Client:
    def __init__(self, result):
        self.result, self.calls = result, []
    def get(self, path, params=None):
        self.calls.append(path)
        return self.result


class TurnoverTests(unittest.TestCase):
    def test_bulk_turnover_only_attached_to_verified_symbols(self):
        snapshot = {"markets": {"KRW-BTC": {
            "bitget": {"match": {"status": "VERIFIED", "symbol": "BTCUSDT"}},
            "gate": {"match": {"status": "UNVERIFIED", "symbol": "BTC_USDT"}},
            "kucoin": {"match": {"status": "VERIFIED", "symbol": "XBTUSDTM"}}}}}
        clients = {"bitget": Client({"data": [{"symbol": "BTCUSDT", "usdtVolume": "123"}]}),
                   "gate": Client([]), "kucoin": Client({"data": [{"symbol": "XBTUSDTM",
                       "quoteCurrency": "USDT", "settleCurrency": "USDT", "isInverse": False,
                       "turnoverOf24h": 456}]})}
        add_turnover(snapshot, clients=clients, clock=lambda: datetime(2026,10,8,tzinfo=UTC))
        pair = snapshot["markets"]["KRW-BTC"]
        self.assertEqual(123, pair["bitget"]["turnover_24h"]["value"])
        self.assertEqual(456, pair["kucoin"]["turnover_24h"]["value"])
        self.assertNotIn("turnover_24h", pair["gate"])
        self.assertEqual([], clients["gate"].calls)

    def test_bad_or_inverse_turnover_is_not_fabricated(self):
        for value in (None, "NaN", "-5"):
            snapshot = {"markets": {"KRW-BTC": {"bitget": {"match": {"status": "VERIFIED", "symbol": "BTCUSDT"}}}}}
            add_turnover(snapshot, clients={"bitget": Client({"data": [{"symbol": "BTCUSDT", "usdtVolume": value}]})})
            self.assertIsNone(snapshot["markets"]["KRW-BTC"]["bitget"]["turnover_24h"]["value"])

    def test_disabled_legacy_endpoints_are_never_called(self):
        a, b = Client(None), Client(None)
        snapshot = build_derivatives_snapshot({"markets": [{"market": "KRW-BTC"}]}, a, b, legacy_providers=False)
        self.assertEqual([], a.calls + b.calls)
        self.assertEqual({}, snapshot["markets"]["KRW-BTC"])
        self.assertEqual(0, snapshot["summary"]["failed_market_count"])
