"""Conservative cross-reference of Upbit assets and Bitget futures underlyings.

Bitget's futures catalog identifies a base ticker, not an asset name or a
globally portable ID. We therefore require an active Bitget coin ID and an
independent CoinGecko ID whose symbol and full name agree with Upbit. A
duplicate symbol additionally requires an overlapping official contract
address. Failure to fetch either catalog never promotes a ticker to verified.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from collector.derivatives import PublicClient, REVIEWED_IDENTITIES, _contract_address, _normal_name


REFERENCE_URL = "https://api.coingecko.com"


class CoinGeckoClient(PublicClient):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(REFERENCE_URL, attempts=2, timeout=25, interval=0.3, **kwargs)
        self.http_attempt_count = 0

    def get(self, path: str, params: dict | None = None, headers: dict | None = None) -> Any:
        return super().get(path, params, {
            "User-Agent": "coincheck-collector/1.0 (public asset identity verification)",
            **(headers or {}),
        })


def _addresses(row: dict) -> set[str]:
    return {address for value in (row.get("platforms") or {}).values()
            if (address := _contract_address(value))}


def _bitget_addresses(row: dict) -> set[str]:
    return {address for chain in row.get("chains") or [] if isinstance(chain, dict)
            if (address := _contract_address(chain.get("contractAddress")))}


def resolve_identities(latest: dict, bitget_coins: list[dict] | None,
                       references: list[dict] | None,
                       identity_cache: dict | None = None) -> tuple[dict[str, dict], dict]:
    """Return auditable identity evidence, never a symbol-only assertion."""
    by_coin: dict[str, list[dict]] = defaultdict(list)
    for row in bitget_coins or []:
        if isinstance(row, dict) and isinstance(row.get("coin"), str):
            by_coin[row["coin"].upper()].append(row)
    by_symbol: dict[str, list[dict]] = defaultdict(list)
    for row in references or []:
        if isinstance(row, dict) and isinstance(row.get("symbol"), str):
            by_symbol[row["symbol"].upper()].append(row)
    resolved: dict[str, dict] = {}
    reasons: dict[str, int] = defaultdict(int)
    for market in latest["markets"]:
        code = market["market"]
        asset = code.removeprefix("KRW-")
        upbit_name = _normal_name((market.get("metadata") or {}).get("english_name"))
        if identity_cache:
            from collector.asset_identity import verify_cached
            coins = by_coin.get(asset, [])
            coin = coins[0] if len(coins) == 1 else None
            cached, category = verify_cached(
                code, (market.get("metadata") or {}).get("english_name"),
                "bitget", coin, identity_cache)
            if cached:
                resolved[code] = cached
                reasons[cached["method"]] += 1
                continue
            reasons[category] += 1
            if category in ("DIFFERENT_ASSET", "CONTRACT_ADDRESS_MISMATCH"):
                continue
        reviewed = REVIEWED_IDENTITIES.get(asset)
        if reviewed and upbit_name == _normal_name(reviewed):
            resolved[code] = {"method": "REVIEWED", "upbit_name": upbit_name}
            reasons["REVIEWED"] += 1
            continue
        if bitget_coins is None or references is None:
            reasons["REFERENCE_UNAVAILABLE"] += 1
            continue
        coins = by_coin.get(asset, [])
        if len(coins) != 1 or not str(coins[0].get("coinId") or ""):
            reasons["BITGET_COIN_ID_MISSING_OR_AMBIGUOUS"] += 1
            continue
        candidates = by_symbol.get(asset, [])
        named = [row for row in candidates if _normal_name(row.get("name")) == upbit_name
                 and row.get("id")]
        if len(named) != 1:
            reasons["REFERENCE_NAME_MISSING_OR_AMBIGUOUS"] += 1
            continue
        reference = named[0]
        bitget_contracts = _bitget_addresses(coins[0])
        reference_contracts = _addresses(reference)
        matching = bitget_contracts & reference_contracts
        if matching:
            method = "CONTRACT_AND_NAME"
        elif len(candidates) == 1 and not (bitget_contracts and reference_contracts):
            method = "UNIQUE_SYMBOL_AND_NAME"
        else:
            reasons["DUPLICATE_SYMBOL_OR_CONTRACT_MISMATCH"] += 1
            continue
        resolved[code] = {"method": method, "upbit_name": upbit_name,
                          "bitget_coin_id": str(coins[0]["coinId"]),
                          "coingecko_id": str(reference["id"]),
                          "contract_address": sorted(matching)[0] if matching else None}
        reasons[method] += 1
    return resolved, {"resolved_count": len(resolved), "reasons": dict(reasons),
                      "reference": "CoinGecko coins/list + Bitget spot/public/coins",
                      "checked_at_utc": datetime.now(UTC).isoformat().replace("+00:00", "Z")}
