"""Cached, exchange-specific asset identity evidence.

CoinGecko exchange tickers associate a spot pair with a CoinGecko asset ID.
This is used only alongside each exchange's own currency and live futures
contract metadata. A ticker match or another exchange's VERIFIED result alone
never establishes identity.
"""

from __future__ import annotations

import json
import time
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

from collector.bitget_identity import CoinGeckoClient
from collector.derivatives import DerivativesAPIError, _contract_address, _normal_name
from collector.main import write_json_atomic


CACHE_VERSION = "1.2"
MAX_AGE = timedelta(hours=24)
REFERENCE_FALLBACK_AGE = timedelta(days=7)
EXCHANGE_IDS = {"upbit": "upbit", "bitget": "bitget", "gate": "gate", "kucoin": "kucoin"}
SOURCE_URL = "https://api.coingecko.com/api/v3/exchanges/{exchange}/tickers"
# A reviewed provider page can disambiguate a spot currency record when its
# API omits a usable contract address. The live futures contract must still
# pass the provider-specific status/base/unit checks before data is collected.
PROVIDER_REVIEWED_SOURCES = {
    ("gate", "ARX"): {
        "coingecko_id": "arcium", "name": "Arcium",
        "url": "https://www.gate.com/pt/futures/USDT/ARX_USDT",
    },
}


def _now() -> datetime:
    return datetime.now(UTC)


def _parse_time(value: str | None) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(UTC) if parsed.tzinfo else None
    except (AttributeError, TypeError, ValueError):
        return None


def load_cache(path: Path, *, now: datetime | None = None) -> dict | None:
    try:
        with path.open(encoding="utf-8") as handle:
            cache = json.load(handle)
    except (OSError, ValueError):
        return None
    checked = _parse_time(cache.get("checked_at_utc"))
    if cache.get("schema_version") != CACHE_VERSION or checked is None:
        return None
    age = (now or _now()) - checked
    return cache if timedelta(0) <= age < MAX_AGE else None


def _ticker_ids(client, exchange: str, *, coin_ids: list[str] | None = None,
                max_pages: int = 1, sleeper=time.sleep) -> tuple[dict[str, str], dict]:
    """Keep only unambiguous base/quote -> CoinGecko ID associations."""
    seen: dict[str, set[str]] = defaultdict(set)
    pages = 0
    error = None
    complete = False
    for page in range(1, max_pages + 1):
        params = {"page": page, "order": "base_target"}
        if coin_ids:
            params["coin_ids"] = ",".join(coin_ids)
        try:
            payload = client.get(f"/api/v3/exchanges/{exchange}/tickers", params)
            rows = payload.get("tickers") if isinstance(payload, dict) else None
            if not isinstance(rows, list):
                raise ValueError("tickers response is not an array")
        except (DerivativesAPIError, ValueError) as exc:
            error = str(exc)[:180]
            break
        pages += 1
        for row in rows:
            if not isinstance(row, dict) or row.get("is_stale") is True:
                continue
            base, quote, coin_id = row.get("base"), row.get("target"), row.get("coin_id")
            if isinstance(base, str) and quote in ("KRW", "USDT") and isinstance(coin_id, str):
                seen[f"{base.upper()}/{quote}"].add(coin_id)
        if len(rows) < 100:
            complete = True
            break
        sleeper(2.1)  # Free public API: avoid a burst of pages.
    unique = {pair: next(iter(ids)) for pair, ids in seen.items() if len(ids) == 1}
    collisions = sorted(pair for pair, ids in seen.items() if len(ids) > 1)
    return unique, {"pages": pages, "complete": complete, "error": error,
                    "collisions": collisions,
                    "url": SOURCE_URL.format(exchange=exchange)}


def _catalog(call, field: str | None = None) -> tuple[list[dict] | None, str | None]:
    try:
        payload = call()
        rows = payload.get(field) if field else payload
        if not isinstance(rows, list):
            raise ValueError("catalog response is not an array")
        return rows, None
    except (DerivativesAPIError, ValueError, TypeError) as exc:
        return None, str(exc)[:180]


def refresh_cache(latest: dict, *, cg=None, bitget=None, gate=None, kucoin=None,
                  previous: dict | None = None, sleeper=time.sleep) -> dict:
    from collector.bitget_derivatives import BitgetClient
    from collector.optional_derivatives import GATE_HOST, KUCOIN_UTA, MeteredClient
    cg = cg or CoinGeckoClient()
    bitget = bitget or BitgetClient()
    gate = gate or MeteredClient(GATE_HOST)
    kucoin = kucoin or MeteredClient(KUCOIN_UTA)
    codes = {row["market"].removeprefix("KRW-") for row in latest["markets"]}
    references, ref_error = _catalog(
        lambda: cg.get("/api/v3/coins/list", {"include_platform": "true"}))
    old_checked = _parse_time((previous or {}).get("checked_at_utc"))
    old_references = (previous or {}).get("references")
    reference_fallback = bool(ref_error and old_checked and
                              timedelta(0) <= _now() - old_checked <= REFERENCE_FALLBACK_AGE and
                              isinstance(old_references, list) and old_references)
    if reference_fallback:
        references = old_references
    coin_rows, bitget_error = _catalog(
        lambda: bitget.get("/api/v2/spot/public/coins"), "data")
    gate_rows, gate_error = _catalog(lambda: gate.get("/spot/currencies"))
    kucoin_rows, kucoin_error = _catalog(
        lambda: kucoin.get("/api/v3/currencies"), "data")
    upbit_ids, upbit_meta = _ticker_ids(cg, "upbit", sleeper=sleeper)
    ticker_ids = {"upbit": upbit_ids}
    ticker_meta = {"upbit": upbit_meta}
    # CoinGecko's free exchange ticker endpoint is shared and rate limited.
    # One page per venue is auxiliary evidence; never treat a partial catalog
    # as complete or use an absent pair as negative identity evidence.
    for provider in ("bitget", "gate", "kucoin"):
        all_ids, ticker_meta[provider] = _ticker_ids(
            cg, provider, sleeper=sleeper)
        ticker_ids[provider] = {pair: coin_id for pair, coin_id in all_ids.items()
                                if pair.split("/")[0] in codes}
    return {
        "schema_version": CACHE_VERSION,
        "checked_at_utc": _now().isoformat().replace("+00:00", "Z"),
        "ticker_ids": ticker_ids,
        "ticker_meta": ticker_meta,
        "references": [row for row in references or [] if
                       isinstance(row, dict) and str(row.get("symbol", "")).upper() in codes],
        "reference_fallback_at_utc": (previous or {}).get("checked_at_utc") if reference_fallback else None,
        "bitget_coins": [row for row in coin_rows or [] if
                        isinstance(row, dict) and row.get("coin") in codes],
        "gate_currencies": [row for row in gate_rows or [] if
                            isinstance(row, dict) and row.get("currency") in codes],
        "kucoin_currencies": [row for row in kucoin_rows or [] if
                              isinstance(row, dict) and row.get("currency") in codes],
        "catalog_errors": {"coingecko": ref_error, "bitget": bitget_error,
                           "gate": gate_error, "kucoin": kucoin_error},
        "request_counts": {"coingecko": getattr(cg, "http_attempt_count", None),
                           "bitget": getattr(bitget, "http_attempt_count", None),
                           "gate": getattr(gate, "http_attempt_count", None),
                           "kucoin": getattr(kucoin, "http_attempt_count", None)},
    }


def _addresses(row: dict | None, provider: str) -> set[str]:
    if not isinstance(row, dict):
        return set()
    if provider == "coingecko":
        return {address for value in (row.get("platforms") or {}).values()
                if (address := _contract_address(value))}
    key = "addr" if provider == "gate" else "contractAddress"
    return {address for chain in row.get("chains") or [] if isinstance(chain, dict)
            if (address := _contract_address(chain.get(key)))}


def _provider_name(currency: dict, provider: str) -> str | None:
    """Return an exchange supplied project name, excluding ticker-only labels."""
    fields = {
        "bitget": ("fullName", "full_name", "coinName", "coin_name", "name"),
        "gate": ("name", "fullName"),
        "kucoin": ("fullName", "full_name", "name"),
    }[provider]
    for field in fields:
        value = currency.get(field)
        if isinstance(value, str) and _normal_name(value):
            # Exchange schemas sometimes put the ticker in either name field.
            # Ignore those labels and keep looking for an actual project name.
            if _normal_name(value) == _normal_name(currency.get("currency")):
                continue
            return value
    return None


def verify_cached(code: str, upbit_name: str, provider: str, currency: dict | None,
                  cache: dict | None) -> tuple[dict | None, str]:
    """Verify exchange metadata and project names; use IDs/addresses as support."""
    if not cache or provider not in ("bitget", "gate", "kucoin"):
        return None, "COINGECKO_ID_UNCONFIRMED"
    asset = code.removeprefix("KRW-")
    if not isinstance(currency, dict):
        if (cache.get("catalog_errors") or {}).get(provider):
            return None, "API_FETCH_FAILED"
        return None, "EXCHANGE_METADATA_MISSING"
    if provider == "bitget":
        if currency.get("coin") != asset or not str(currency.get("coinId") or ""):
            return None, "EXCHANGE_METADATA_MISSING"
    elif currency.get("currency") != asset:
        return None, "EXCHANGE_METADATA_MISSING"
    candidates = [row for row in cache.get("references") or []
                  if isinstance(row, dict) and
                  str(row.get("symbol", "")).upper() == asset]
    named = [row for row in candidates
             if _normal_name(row.get("name")) == _normal_name(upbit_name)]
    official_addresses = _addresses(currency, provider)
    provider_name = _provider_name(currency, provider)
    names_match = bool(_normal_name(upbit_name) and provider_name and
                       _normal_name(provider_name) == _normal_name(upbit_name))
    reference_ids = {str(row["id"]) for row in named if row.get("id")}
    named_addresses = set().union(*(_addresses(row, "coingecko") for row in named)) if named else set()
    if names_match:
        # Names plus the exchange's own asset record are sufficient. CoinGecko
        # IDs and addresses are optional, but conflicting confirmed addresses
        # remain a hard veto.
        if official_addresses and named_addresses and not official_addresses & named_addresses:
            return None, "CONTRACT_ADDRESS_MISMATCH"
        reference_id = next(iter(reference_ids)) if len(reference_ids) == 1 else None
        return {"method": "OFFICIAL_SYMBOL_AND_PROJECT_NAME",
                "coingecko_id": reference_id,
                "upbit_name": _normal_name(upbit_name), "provider": provider,
                "provider_coin_id": str(currency.get("coinId")) if provider == "bitget" else None,
                "contract_address": sorted(official_addresses & named_addresses)[0]
                if official_addresses & named_addresses else None,
                "evidence": [{"source": "Upbit market metadata", "name": upbit_name},
                             {"source": "official exchange asset metadata",
                              "name": provider_name, "symbol": asset},
                             {"source": "official exchange currency catalog",
                              "provider": provider}]}, "VERIFIED"

    # Bitget's public coin catalog often supplies a numeric coinId and chain
    # data but no project name. In that case, a unique exact-name reference
    # tied to Bitget's own coin record is acceptable without requiring its
    # CoinGecko ID or a contract address.
    if provider == "bitget" and not provider_name and named and len(reference_ids) <= 1:
        if official_addresses and named_addresses and not official_addresses & named_addresses:
            return None, "CONTRACT_ADDRESS_MISMATCH"
        reference_id = next(iter(reference_ids)) if len(reference_ids) == 1 else None
        return {"method": "OFFICIAL_SYMBOL_AND_REFERENCE_NAME",
                "coingecko_id": reference_id,
                "upbit_name": _normal_name(upbit_name), "provider": provider,
                "provider_coin_id": str(currency.get("coinId")),
                "contract_address": sorted(official_addresses & named_addresses)[0]
                if official_addresses & named_addresses else None,
                "evidence": ["https://api.bitget.com/api/v2/spot/public/coins",
                             "https://api.coingecko.com/api/v3/coins/list?include_platform=true"]}, "VERIFIED"

    # Preserve contract based corroboration when exchange display names differ.
    # This is the supplementary path for aliases and common naming variants.
    if named:
        overlaps = set().union(*(_addresses(row, "coingecko") for row in named)) & official_addresses
        if overlaps:
            reference_id = next(iter(reference_ids)) if len(reference_ids) == 1 else None
            return {"method": "OFFICIAL_CONTRACT_AND_REFERENCE_NAME",
                    "coingecko_id": reference_id,
                    "upbit_name": _normal_name(upbit_name), "provider": provider,
                    "provider_coin_id": str(currency.get("coinId")) if provider == "bitget" else None,
                    "contract_address": sorted(overlaps)[0],
                    "evidence": ["https://api.coingecko.com/api/v3/coins/list?include_platform=true",
                                 {"bitget": "https://api.bitget.com/api/v2/spot/public/coins",
                                  "gate": "https://api.gateio.ws/api/v4/spot/currencies",
                                  "kucoin": "https://api.kucoin.com/api/v3/currencies"}[provider]]}, "VERIFIED"
        if official_addresses and named_addresses:
            return None, "CONTRACT_ADDRESS_MISMATCH"
    reviewed = PROVIDER_REVIEWED_SOURCES.get((provider, asset))
    if reviewed and len(named) == 1 and named[0]["id"] == reviewed["coingecko_id"] \
            and _normal_name(upbit_name) == _normal_name(reviewed["name"]) \
            and _normal_name(currency.get("name")) == _normal_name(reviewed["name"]):
        ids = cache.get("ticker_ids") or {}
        if any(observed and observed != reviewed["coingecko_id"] for observed in (
                (ids.get("upbit") or {}).get(f"{asset}/KRW"),
                (ids.get(provider) or {}).get(f"{asset}/USDT"))):
            return None, "DIFFERENT_ASSET"
        reference_addresses = _addresses(named[0], "coingecko")
        if reference_addresses and official_addresses and not reference_addresses & official_addresses:
            return None, "CONTRACT_ADDRESS_MISMATCH"
        return {"method": "REVIEWED_OFFICIAL_FUTURES_PAGE_AND_CURRENCY_NAME",
                "coingecko_id": reviewed["coingecko_id"],
                "upbit_name": _normal_name(upbit_name), "provider": provider,
                "contract_address": None,
                "evidence": [reviewed["url"],
                             "https://api.gateio.ws/api/v4/spot/currencies",
                             "https://api.coingecko.com/api/v3/coins/list?include_platform=true"]}, "VERIFIED"
    # Different names require stronger independent identity evidence.
    metadata = cache.get("ticker_meta") or {}
    if any((metadata.get(exchange) or {}).get("error")
           for exchange in ("upbit", provider)):
        return None, "API_FETCH_FAILED"
    if not all((metadata.get(exchange) or {}).get("complete") is True
               for exchange in ("upbit", provider)):
        return None, "COINGECKO_ID_UNCONFIRMED"
    if (f"{asset}/KRW" in (metadata.get("upbit") or {}).get("collisions", []) or
            f"{asset}/USDT" in (metadata.get(provider) or {}).get("collisions", [])):
        return None, "SYMBOL_COLLISION"
    ids = cache.get("ticker_ids") or {}
    upbit_id = (ids.get("upbit") or {}).get(f"{asset}/KRW")
    provider_id = (ids.get(provider) or {}).get(f"{asset}/USDT")
    if not upbit_id or not provider_id:
        return None, "COINGECKO_ID_UNCONFIRMED"
    if upbit_id != provider_id:
        return None, "DIFFERENT_ASSET"
    refs = [row for row in cache.get("references") or []
            if row.get("id") == upbit_id and str(row.get("symbol", "")).upper() == asset]
    if len(refs) != 1:
        return None, "COINGECKO_ID_UNCONFIRMED"
    reference = refs[0]
    cg_addresses = _addresses(reference, "coingecko")
    official_addresses = _addresses(currency, provider)
    overlap = cg_addresses & official_addresses
    if cg_addresses and official_addresses and not overlap:
        return None, "CONTRACT_ADDRESS_MISMATCH"
    if provider != "bitget":
        official_name = currency.get("name") if provider == "gate" else currency.get("fullName")
        if not (_normal_name(official_name) and
                (_normal_name(official_name) in
                 {_normal_name(upbit_name), _normal_name(reference.get("name"))} or overlap)):
            return None, "ASSET_NAME_MISMATCH"
    method = "OFFICIAL_CONTRACT_AND_EXCHANGE_ID" if overlap else "EXCHANGE_SPOT_ID_AND_METADATA"
    return {"method": method, "coingecko_id": upbit_id,
            "upbit_name": _normal_name(upbit_name),
            "provider": provider,
            "provider_coin_id": str(currency.get("coinId")) if provider == "bitget" else None,
            "contract_address": sorted(overlap)[0] if overlap else None,
            "evidence": [
                SOURCE_URL.format(exchange="upbit"),
                SOURCE_URL.format(exchange=provider),
                "https://api.coingecko.com/api/v3/coins/list?include_platform=true",
                {"bitget": "https://api.bitget.com/api/v2/spot/public/coins",
                 "gate": "https://api.gateio.ws/api/v4/spot/currencies",
                 "kucoin": "https://api.kucoin.com/api/v3/currencies"}[provider],
            ]}, "VERIFIED"


def diagnose_unverified(code: str, upbit_name: str, provider: str,
                        currency: dict | None, cache: dict | None) -> tuple[str, dict]:
    """Record bounded, reproducible evidence without promoting an asset."""
    asset = code.removeprefix("KRW-")
    if not cache:
        return "API_FETCH_FAILED", {"missing": "identity_cache"}
    if not isinstance(currency, dict):
        error = (cache.get("catalog_errors") or {}).get(provider)
        return ("API_FETCH_FAILED" if error else "EXCHANGE_METADATA_MISSING"), {
            "catalog_error": error}
    refs = [row for row in cache.get("references") or []
            if isinstance(row, dict) and str(row.get("symbol", "")).upper() == asset]
    names = [row for row in refs if _normal_name(row.get("name")) == _normal_name(upbit_name)]
    provider_name = currency.get("name") if provider == "gate" else currency.get("fullName")
    provider_addresses = _addresses(currency, provider)
    named_addresses = set().union(*(_addresses(row, "coingecko") for row in names)) if names else set()
    evidence = {"upbit_name": upbit_name, "provider_name": provider_name,
                "coingecko_candidates": [{"id": row.get("id"), "name": row.get("name")}
                                         for row in refs[:8]],
                "provider_contracts": sorted(provider_addresses)[:4],
                "reference_contracts": sorted(named_addresses)[:4],
                "ticker_error": (cache.get("ticker_meta") or {}).get(provider, {}).get("error")}
    if len(names) > 1:
        return "SYMBOL_COLLISION", evidence
    if len(names) == 1 and provider_addresses and named_addresses and not provider_addresses & named_addresses:
        return "CONTRACT_ADDRESS_MISMATCH", evidence
    if len(refs) > 1 and not provider_addresses & named_addresses:
        return "SYMBOL_COLLISION", evidence
    if not names:
        return ("ASSET_NAME_MISMATCH" if refs else "COINGECKO_ID_UNCONFIRMED"), evidence
    if provider_name and _normal_name(provider_name) != _normal_name(upbit_name):
        return "ASSET_NAME_MISMATCH", evidence
    if (cache.get("ticker_meta") or {}).get(provider, {}).get("error"):
        return "API_FETCH_FAILED", evidence
    return "COINGECKO_ID_UNCONFIRMED", evidence


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--latest", type=Path, default=Path("data/latest.json"))
    parser.add_argument("--output", type=Path, default=Path("data/asset_identity_cache.json"))
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    if not args.force and load_cache(args.output):
        print("Fresh asset identity cache retained")
        return 0
    with args.latest.open(encoding="utf-8") as handle:
        latest = json.load(handle)
    try:
        with args.output.open(encoding="utf-8") as handle:
            previous = json.load(handle)
    except (OSError, ValueError):
        previous = None
    cache = refresh_cache(latest, previous=previous)
    write_json_atomic(cache, args.output, compact=True)
    print("Refreshed asset identity cache:",
          {provider: len(rows) for provider, rows in cache["ticker_ids"].items()},
          cache["catalog_errors"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
