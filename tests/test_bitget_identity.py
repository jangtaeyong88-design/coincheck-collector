import unittest

from collector.bitget_identity import resolve_identities


def latest(code="ABC", name="Alpha Beta Coin"):
    return {"markets": [{"market": f"KRW-{code}",
                         "metadata": {"english_name": name}}]}


class IdentityTests(unittest.TestCase):
    def test_unique_symbol_requires_upbit_name_and_bitget_coin_id(self):
        references = [{"id": "alpha-beta", "symbol": "abc",
                       "name": "Alpha Beta Coin", "platforms": {}}]
        coins = [{"coin": "ABC", "coinId": "123", "chains": []}]
        identities, summary = resolve_identities(latest(), coins, references)
        self.assertEqual(identities["KRW-ABC"]["coingecko_id"], "alpha-beta")
        self.assertEqual(identities["KRW-ABC"]["method"], "UNIQUE_SYMBOL_AND_NAME")
        self.assertEqual(summary["resolved_count"], 1)
        self.assertEqual(resolve_identities(latest(name="Different"), coins, references)[0], {})
        self.assertEqual(resolve_identities(latest(), [{"coin": "ABC"}], references)[0], {})

    def test_duplicate_symbol_only_passes_on_matching_contract(self):
        references = [
            {"id": "alpha-beta", "symbol": "abc", "name": "Alpha Beta Coin",
             "platforms": {"ethereum": "0x" + "1234567890abcdef" * 2 + "12345678"}},
            {"id": "other", "symbol": "abc", "name": "Other Coin", "platforms": {}}]
        coins = [{"coin": "ABC", "coinId": "123", "chains": [
            {"chain": "ETH", "contractAddress": "0x" + "1234567890ABCDEF" * 2 + "12345678"}]}]
        identities, _ = resolve_identities(latest(), coins, references)
        self.assertEqual(identities["KRW-ABC"]["method"], "CONTRACT_AND_NAME")
        coins[0]["chains"][0]["contractAddress"] = "0x" + "9" * 40
        self.assertEqual(resolve_identities(latest(), coins, references)[0], {})

    def test_missing_external_catalog_never_promotes_unreviewed_symbol(self):
        self.assertEqual(resolve_identities(latest(), None, None)[0], {})
        reviewed, _ = resolve_identities(latest("BTC", "Bitcoin"), None, None)
        self.assertEqual(reviewed["KRW-BTC"]["method"], "REVIEWED")


if __name__ == "__main__":
    unittest.main()
