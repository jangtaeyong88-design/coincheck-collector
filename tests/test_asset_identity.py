"""Provider identity checks require exchange metadata, not a ticker alone."""

import unittest

from collector.asset_identity import _ticker_ids, verify_cached


class TickerClient:
    def get(self, path, params):
        if params["page"] == 1:
            return {"tickers": [{"base": "ABC", "target": "KRW", "coin_id": "alpha",
                                 "is_stale": False},
                                {"base": "ABC", "target": "KRW", "coin_id": "old",
                                 "is_stale": True}]}
        return {"tickers": []}


class AssetIdentityTest(unittest.TestCase):
    def test_ticker_catalog_rejects_stale_pair_evidence(self):
        ids, diagnostics = _ticker_ids(TickerClient(), "upbit", sleeper=lambda _: None)
        self.assertEqual(ids, {"ABC/KRW": "alpha"})
        self.assertTrue(diagnostics["complete"])

    def test_matching_official_name_does_not_require_external_id_or_address(self):
        identity, reason = verify_cached("KRW-ABC", "Alpha", "gate",
                                         {"currency": "ABC", "name": "Alpha"},
                                         {"references": []})
        self.assertEqual(reason, "VERIFIED")
        self.assertEqual(identity["method"], "OFFICIAL_SYMBOL_AND_PROJECT_NAME")
        self.assertIsNone(identity["contract_address"])

    def test_missing_exchange_metadata_never_promotes_ticker(self):
        identity, reason = verify_cached("KRW-ABC", "Alpha", "gate", None,
                                         {"references": []})
        self.assertIsNone(identity)
        self.assertEqual(reason, "EXCHANGE_METADATA_MISSING")

    def test_confirmed_contract_mismatch_blocks_matching_name(self):
        address_a = "0x" + "1" * 40
        address_b = "0x" + "2" * 40
        cache = {"references": [{"id": "alpha", "symbol": "abc", "name": "Alpha",
                                 "platforms": {"ethereum": address_a}}]}
        identity, reason = verify_cached("KRW-ABC", "Alpha", "gate",
                                         {"currency": "ABC", "name": "Alpha",
                                          "chains": [{"addr": address_b}]}, cache)
        self.assertIsNone(identity)
        self.assertEqual(reason, "CONTRACT_ADDRESS_MISMATCH")


if __name__ == "__main__":
    unittest.main()
