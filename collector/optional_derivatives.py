"""Selective, separately attributed Gate and KuCoin futures observations."""

from __future__ import annotations

import time
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any, Callable

from collector.bitget_identity import CoinGeckoClient
from collector.derivatives import (DerivativesAPIError, PublicClient, REVIEWED_IDENTITIES,
                                   _contract_address, _empty_data, _normal_name, _number, _timestamp,
                                   _unavailable_data)
from collector.collection_targets import eligible_markets


GATE_HOST = "https://api.gateio.ws/api/v4"
KUCOIN_HOST = "https://api-futures.kucoin.com"
KUCOIN_UTA = "https://api.kucoin.com"
HOUR_MS = 3_600_000
TOLERANCE_MS = 10 * 60_000
GATE_LIMIT = 80
KUCOIN_LIMIT = 55


def _rotate_targets(targets: list[tuple], cap: int) -> list[tuple]:
    """Rotate contract-bearing markets fairly within a bounded API budget."""
    present = [item for item in targets if item[2] is not None]
    absent = [item for item in targets if item[2] is None]
    if len(present) > cap:
        offset = (int(time.time()) // (4 * 3600) * cap) % len(present)
        present = present[offset:] + present[:offset]
    return present + absent


class MeteredClient(PublicClient):
    def __init__(self, host: str, **kwargs: Any) -> None:
        super().__init__(host, interval=0.18, **kwargs)
        self.request_count = 0
        self.http_attempt_count = 0
        self.request_duration_seconds = 0.0

    def get(self, path: str, params: dict | None = None, headers: dict | None = None) -> Any:
        self.request_count += 1
        started = time.monotonic()
        try:
            payload = super().get(path, params, headers)
        finally:
            self.request_duration_seconds += time.monotonic() - started
        if self.base_url.startswith("https://api.kucoin.com") or self.base_url.startswith("https://api-futures.kucoin.com"):
            if not isinstance(payload, dict) or payload.get("code") != "200000":
                raise DerivativesAPIError(f"KuCoin {path}: invalid API code",
                                          details={"path": path, "category": "EXCHANGE_ERROR",
                                                   "exchange_code": payload.get("code") if isinstance(payload, dict) else None})
        return payload


def _try(data: dict, stage: str, call) -> Any:
    try:
        return call()
    except (DerivativesAPIError, ValueError, TypeError, KeyError) as exc:
        data["errors"].append({"stage": stage, "reason": str(exc)[:180],
                               "details": getattr(exc, "details", {})})
        return None


def _references(client: CoinGeckoClient | None, diagnostics: dict) -> list[dict] | None:
    try:
        rows = (client or CoinGeckoClient()).get("/api/v3/coins/list", {"include_platform": "true"})
        if not isinstance(rows, list):
            raise ValueError("CoinGecko list is not an array")
        return rows
    except (DerivativesAPIError, ValueError) as exc:
        diagnostics["coingecko"] = str(exc)[:180]
        return None


def _addresses(row: dict, provider: str) -> set[str]:
    chains = row.get("chains") or []
    field = "addr" if provider == "gate" else "contractAddress"
    return {address for chain in chains if isinstance(chain, dict)
            if (address := _contract_address(chain.get(field)))}


def _verified_identity(code: str, metadata: dict, currency: dict | None,
                       references: list[dict] | None, provider: str,
                       bitget_match: dict | None,
                       identity_cache: dict | None = None) -> dict | None:
    asset = code.removeprefix("KRW-")
    name = _normal_name(metadata.get("english_name"))
    if currency is None:
        return None
    if identity_cache:
        from collector.asset_identity import verify_cached
        cached, category = verify_cached(code, metadata.get("english_name"),
                                         provider, currency, identity_cache)
        if cached:
            return cached
        if category in ("DIFFERENT_ASSET", "CONTRACT_ADDRESS_MISMATCH"):
            return None
    reviewed = REVIEWED_IDENTITIES.get(asset)
    if reviewed and name == _normal_name(reviewed):
        return {"method": "REVIEWED", "upbit_name": name}
    currency_name = currency.get("name") if provider == "gate" else currency.get("fullName")
    if not name:
        return None
    if (bitget_match or {}).get("status") == "VERIFIED" and (bitget_match or {}).get("identity"):
        known_address = _contract_address(bitget_match["identity"].get("contract_address"))
        provider_addresses = _addresses(currency, provider)
        if known_address and provider_addresses:
            if known_address not in provider_addresses:
                return None
            return {"method": "CROSS_CHECKED_OFFICIAL_CONTRACT", "upbit_name": name,
                    "contract_address": known_address}
    if references is None:
        return None
    candidates = [row for row in references if isinstance(row, dict)
                  and str(row.get("symbol", "")).upper() == asset]
    named = [row for row in candidates if _normal_name(row.get("name")) == name and row.get("id")]
    if len(named) != 1:
        return None
    ref_addresses = {address for value in (named[0].get("platforms") or {}).values()
                     if (address := _contract_address(value))}
    common = _addresses(currency, provider) & ref_addresses
    if common:
        return {"method": "CONTRACT_AND_NAME", "coingecko_id": named[0]["id"],
                "contract_address": sorted(common)[0]}
    if _normal_name(currency_name) != name:
        return None
    if len(candidates) == 1 and not (_addresses(currency, provider) and ref_addresses):
        return {"method": "UNIQUE_THREE_WAY_NAME", "coingecko_id": named[0]["id"]}
    return None


def _hourly_changes(rows: list[dict], value_key: str, time_key: str,
                    *, seconds: bool = False, require_fresh: bool = False) -> tuple[dict, int | None]:
    points = sorted(((int(row[time_key]) * (1000 if seconds else 1), _number(row.get(value_key)))
                     for row in rows if isinstance(row, dict) and _timestamp(row.get(time_key)) is not None
                     and _number(row.get(value_key)) is not None), reverse=True)
    newest = points[0] if points else None
    if require_fresh and newest and not (-300_000 <= int(time.time() * 1000) - newest[0] <= 2 * HOUR_MS):
        newest = None
    changes = {}
    for hours in (1, 4, 24):
        result = {"pct": None, "reference_at_ms": None, "elapsed_ms": None,
                  "status": "INSUFFICIENT_HISTORY"}
        if newest and newest[1] is not None:
            target = newest[0] - hours * HOUR_MS
            previous = next(((at, value) for at, value in points[1:]
                             if abs(at - target) <= TOLERANCE_MS and value > 0), None)
            if previous:
                result.update({"pct": round((newest[1] / previous[1] - 1) * 100, 8),
                               "reference_at_ms": previous[0], "elapsed_ms": newest[0] - previous[0],
                               "status": "AVAILABLE"})
        changes[f"{hours}h"] = result
    return changes, newest[0] if newest else None


def _gate_data(client: MeteredClient, symbol: str, contract: dict,
               catalog_at_ms: int | None = None) -> dict:
    data = _empty_data()
    stats = _try(data, "contract_stats", lambda: client.get(
        "/futures/usdt/contract_stats", {"contract": symbol, "interval": "1h", "limit": 30}))
    now_ms = int(time.time() * 1000)
    count = _number(contract.get("position_size"))
    multiplier = _number(contract.get("quanto_multiplier"))
    mark = _number(contract.get("mark_price"))
    oi = data["oi"]
    oi.update({"status": "AVAILABLE" if count is not None else "FETCH_FAILED",
               "exchange_quantity": count, "exchange_quantity_unit": "CONTRACTS" if count is not None else None,
               "source": "contract_catalog_position_size",
               "contract_quantity": count, "at_ms": (catalog_at_ms or now_ms) if count is not None else None,
               "base_asset_quantity": round(count * multiplier, 8) if count is not None and multiplier else None,
               "value_usdt": round(count * multiplier * mark, 8) if count is not None and multiplier and mark else None,
               "value_at_ms": (catalog_at_ms or now_ms) if count is not None and multiplier and mark else None,
               "mark_price": mark})
    rows = sorted((row for row in stats if isinstance(row, dict) and _timestamp(row.get("time")))
                  if isinstance(stats, list) else [], key=lambda row: int(row["time"]), reverse=True)
    if rows and now_ms - int(rows[0]["time"]) * 1000 <= 2 * HOUR_MS:
        latest = rows[0]
        oi["changes"], oi["changes_source_at_ms"] = _hourly_changes(rows, "open_interest", "time", seconds=True)
        oi["latest_hourly_quantity"] = _number(latest.get("open_interest"))
        oi["latest_hourly_value_usdt"] = _number(latest.get("open_interest_usd"))
        oi["history_status"] = "AVAILABLE" if len(rows) > 1 else "INSUFFICIENT_HISTORY"
        for name, key in (("general_accounts", "lsr_account"),
                          ("top_accounts", "top_lsr_account"), ("top_positions", "top_lsr_size")):
            value = _number(latest.get(key))
            data["long_short"][name] = {"ratio": value, "at_ms": int(latest["time"]) * 1000,
                                        "status": "AVAILABLE" if value is not None else "FETCH_FAILED"}
        for hours in (1, 4, 24):
            selected = rows[:hours]
            complete = len(selected) == hours and all(
                int(selected[i]["time"]) - int(selected[i + 1]["time"]) == 3600
                for i in range(len(selected) - 1))
            item = data["liquidations"][f"{hours}h"]
            for side in ("long", "short"):
                key = f"{side}_liq_usd_new"
                if complete and all(_number(row.get(key)) is not None for row in selected):
                    item[side] = round(sum(_number(row[key]) for row in selected), 8)
            if not complete or any(item[side] is None for side in ("long", "short")):
                data["liquidations"]["status"] = "INSUFFICIENT_HISTORY"
        if all(data["liquidations"][f"{h}h"][side] is not None
               for h in (1, 4, 24) for side in ("long", "short")):
            data["liquidations"]["status"] = "AVAILABLE"
        data["liquidations"]["at_ms"] = int(latest["time"]) * 1000
        data["liquidations"]["scope"] = "symbol"
        data["liquidations"]["aggregation_window"] = "exchange_hourly_stat_buckets"
    else:
        data["oi"]["history_status"] = "FETCH_FAILED" if stats is None else "INSUFFICIENT_HISTORY"
        data["liquidations"]["status"] = "FETCH_FAILED" if stats is None else "INSUFFICIENT_HISTORY"
        if stats is None:
            for name in ("general_accounts", "top_accounts", "top_positions"):
                data["long_short"][name]["status"] = "FETCH_FAILED"
    funding = data["funding"]
    funding["current_rate"] = _number(contract.get("funding_rate"))
    funding["current_observed_at_ms"] = catalog_at_ms or now_ms
    funding["next_at_ms"] = (_timestamp(contract.get("funding_next_apply")) or 0) * 1000 or None
    funding["current_status"] = "AVAILABLE" if funding["current_rate"] is not None else "NOT_SUPPORTED"
    past = _try(data, "funding_history", lambda: client.get(
        "/futures/usdt/funding_rate", {"contract": symbol, "limit": 2}))
    valid = sorted((row for row in past if isinstance(row, dict) and _timestamp(row.get("t")))
                   if isinstance(past, list) else [], key=lambda row: int(row["t"]), reverse=True)
    for label, row in zip(("latest", "previous"), valid):
        funding[f"{label}_rate"] = _number(row.get("r"))
        funding[f"{label}_at_ms"] = int(row["t"]) * 1000
    if len(valid) >= 2 and funding.get("latest_rate") is not None and funding.get("previous_rate") is not None:
        funding["direction"] = ("UP" if funding["latest_rate"] > funding["previous_rate"]
                                else "DOWN" if funding["latest_rate"] < funding["previous_rate"]
                                else "UNCHANGED")
    funding["history_status"] = "AVAILABLE" if valid else "FETCH_FAILED" if past is None else "INSUFFICIENT_HISTORY"
    return data


def _kucoin_data(futures: MeteredClient, uta: MeteredClient, symbol: str, contract: dict,
                 catalog_observed_at_ms: int | None = None) -> dict:
    data = _empty_data()
    current = _try(data, "oi_current", lambda: uta.get(
        "/api/ua/v2/market/open-interest", {"symbol": symbol}))
    current_rows = current.get("data") if isinstance(current, dict) else None
    current_row = next((row for row in current_rows if isinstance(row, dict) and row.get("symbol", symbol) == symbol), None) \
        if isinstance(current_rows, list) else current_rows if isinstance(current_rows, dict) else None
    oi = data["oi"]
    count = _number(current_row.get("openInterest")) if current_row else None
    at = _timestamp(current_row.get("ts")) if current_row else None
    current_observed_at_ms = int(time.time() * 1000)
    catalog_count = _number(contract.get("openInterest"))
    multiplier = _number(contract.get("multiplier"))
    mark = _number(contract.get("markPrice"))
    # KuCoin defines OI/position quantity in contracts and documents multiplier
    # as the underlying quantity represented by one contract. The catalog OI is
    # only a consistency check: its snapshot may be stale relative to UTA OI.
    unit_reasons = []
    if contract.get("symbol") != symbol:
        unit_reasons.append("CONTRACT_SYMBOL_MISMATCH")
    expected_base = symbol.removesuffix("USDTM")
    if contract.get("baseCurrency") != expected_base:
        unit_reasons.append("BASE_CURRENCY_MISMATCH")
    if contract.get("quoteCurrency") != "USDT" or contract.get("settleCurrency") != "USDT":
        unit_reasons.append("NOT_USDT_SETTLED")
    if contract.get("status") != "Open" or contract.get("expireDate") is not None:
        unit_reasons.append("CONTRACT_NOT_ACTIVE_PERPETUAL")
    if contract.get("isInverse") is not False:
        unit_reasons.append("CONTRACT_TYPE_UNCONFIRMED")
    if multiplier is None or multiplier <= 0:
        unit_reasons.append("MULTIPLIER_MISSING_OR_INVALID")
    unit_ok = not unit_reasons
    difference_pct = (abs(count - catalog_count) / catalog_count * 100
                      if count is not None and catalog_count is not None and catalog_count > 0 else None)
    if count is None:
        crosscheck_status = "CURRENT_OI_UNAVAILABLE"
    elif catalog_count is None or catalog_count <= 0:
        crosscheck_status = "CATALOG_OI_UNAVAILABLE"
    elif difference_pct is not None and difference_pct <= 5:
        crosscheck_status = "MATCH"
    else:
        crosscheck_status = "MISMATCH"
    oi.update({"exchange_quantity": count, "exchange_quantity_unit": "CONTRACTS" if unit_ok else "UNCONFIRMED",
               "contract_quantity": count if unit_ok else None,
               "base_asset_quantity": round(count * multiplier, 8)
               if unit_ok and count is not None and multiplier is not None else None,
               "value_usdt": round(count * multiplier * mark, 8)
               if unit_ok and count is not None and multiplier is not None and mark is not None else None,
               "value_at_ms": catalog_observed_at_ms if unit_ok and mark is not None else None, "at_ms": at,
               "unit_verification": "KUCOIN_OFFICIAL_CONTRACT_MULTIPLIER" if unit_ok else "UNCONFIRMED",
               "unit_diagnostics": {
                   "status": "CONFIRMED" if unit_ok else "UNCONFIRMED",
                   "reason": "KuCoin documents openInterest as contract quantity and multiplier as underlying quantity per contract."
                   if unit_ok else ",".join(unit_reasons),
                   "current_oi": {"value": count, "unit": "CONTRACTS" if unit_ok else "UNCONFIRMED",
                                  "source": "KuCoin UTA /api/ua/v2/market/open-interest",
                                  "value_at_ms": at, "observed_at_ms": current_observed_at_ms},
                   "catalog_oi": {"value": catalog_count, "unit": "CONTRACTS" if catalog_count is not None else None,
                                  "source": "KuCoin Futures /api/v1/contracts/active",
                                  "observed_at_ms": catalog_observed_at_ms},
                   "difference_pct": round(difference_pct, 6) if difference_pct is not None else None,
                   "catalog_crosscheck_status": crosscheck_status,
                   "catalog_crosscheck_tolerance_pct": 5,
                   "multiplier": multiplier,
                   "multiplier_unit": "BASE_ASSET_PER_CONTRACT" if unit_ok else None,
                   "multiplier_observed_at_ms": catalog_observed_at_ms,
                   "mark_price": mark,
                   "mark_price_unit": "USDT_PER_BASE_ASSET" if mark is not None else None,
                   "mark_price_observed_at_ms": catalog_observed_at_ms if mark is not None else None,
                   "contract_symbol": contract.get("symbol"),
                   "base_currency": contract.get("baseCurrency"),
                   "quote_currency": contract.get("quoteCurrency"),
                   "settle_currency": contract.get("settleCurrency"),
                   "is_inverse": contract.get("isInverse"),
                   "evidence": ["https://www.kucoin.com/support/26696094882969",
                                "https://www.kucoin.com/docs-new/api-3470220",
                                "https://www.kucoin.com/docs-new/v2/rest/ua/get-futures-open-interest"],
               },
               "status": "AVAILABLE" if count is not None and at is not None else "FETCH_FAILED"})
    history = _try(data, "oi_history", lambda: uta.get(
        "/api/ua/v2/market/open-interest", {"symbol": symbol, "interval": "1hour", "pageSize": 30}))
    rows = history.get("data") if isinstance(history, dict) else None
    oi["changes"], oi["changes_source_at_ms"] = _hourly_changes(
        rows if isinstance(rows, list) else [], "openInterest", "ts", require_fresh=True)
    oi["history_status"] = "AVAILABLE" if isinstance(rows, list) and len(rows) > 1 else \
        "FETCH_FAILED" if history is None else "INSUFFICIENT_HISTORY"
    funding_response = _try(data, "funding_current", lambda: futures.get(
        f"/api/v1/funding-rate/{symbol}/current"))
    funding_row = funding_response.get("data") if isinstance(funding_response, dict) else None
    funding = data["funding"]
    funding["current_rate"] = _number(funding_row.get("value")) if isinstance(funding_row, dict) else None
    funding["current_observed_at_ms"] = int(time.time() * 1000) if funding_row else None
    funding["next_at_ms"] = _timestamp(funding_row.get("fundingTime")) if funding_row else None
    funding["current_status"] = "AVAILABLE" if funding["current_rate"] is not None else "FETCH_FAILED"
    now_ms = int(time.time() * 1000)
    past = _try(data, "funding_history", lambda: futures.get(
        "/api/v1/contract/funding-rates",
        {"symbol": symbol, "from": now_ms - 3 * 86_400_000, "to": now_ms}))
    past_rows = past.get("data") if isinstance(past, dict) else None
    valid = sorted((row for row in past_rows if isinstance(row, dict) and
                    _timestamp(row.get("timepoint")) is not None)
                   if isinstance(past_rows, list) else [],
                   key=lambda row: int(row["timepoint"]), reverse=True)
    for label, row in zip(("latest", "previous"), valid):
        funding[f"{label}_rate"] = _number(row.get("fundingRate"))
        funding[f"{label}_at_ms"] = _timestamp(row.get("timepoint"))
    if len(valid) >= 2 and funding.get("latest_rate") is not None and funding.get("previous_rate") is not None:
        funding["direction"] = ("UP" if funding["latest_rate"] > funding["previous_rate"]
                                else "DOWN" if funding["latest_rate"] < funding["previous_rate"]
                                else "UNCHANGED")
    funding["history_status"] = "AVAILABLE" if valid else "FETCH_FAILED" if past is None else "INSUFFICIENT_HISTORY"
    for name in ("general_accounts", "top_accounts", "top_positions"):
        data["long_short"][name]["status"] = "NOT_SUPPORTED"
    data["liquidations"]["status"] = "NOT_SUPPORTED"
    return data


def _match_gate(code: str, metadata: dict, contract: dict | None, currency: dict | None,
                refs: list[dict] | None, bitget: dict | None,
                identity_cache: dict | None = None) -> dict:
    if not contract:
        return {"status": "ABSENT", "symbol": None}
    asset = code.removeprefix("KRW-")
    symbol = f"{asset}_USDT"
    if contract.get("name") != symbol or contract.get("type") != "direct" or \
            contract.get("status") != "trading" or _number(contract.get("quanto_multiplier")) is None or \
            _number(contract.get("quanto_multiplier")) <= 0:
        return {"status": "UNVERIFIED", "symbol": contract.get("name"), "reason": "contract metadata or unit"}
    identity = _verified_identity(code, metadata, currency, refs, "gate",
                                  (bitget or {}).get("match"), identity_cache)
    return {"status": "VERIFIED", "symbol": symbol, "identity": identity} if identity else \
        {"status": "UNVERIFIED", "symbol": symbol, "reason": "asset identity not independently verified"}


def _match_kucoin(code: str, metadata: dict, contract: dict | None, currency: dict | None,
                  refs: list[dict] | None, bitget: dict | None,
                  identity_cache: dict | None = None) -> dict:
    if not contract:
        return {"status": "ABSENT", "symbol": None}
    asset = code.removeprefix("KRW-")
    base = "XBT" if asset == "BTC" else asset
    symbol = f"{base}USDTM"
    if contract.get("symbol") != symbol or contract.get("baseCurrency") != base or \
            contract.get("quoteCurrency") != "USDT" or contract.get("settleCurrency") != "USDT" or \
            contract.get("status") != "Open" or contract.get("expireDate") is not None or \
            _number(contract.get("multiplier")) is None or _number(contract.get("multiplier")) <= 0:
        return {"status": "UNVERIFIED", "symbol": contract.get("symbol"), "reason": "contract metadata or unit"}
    identity = _verified_identity(code, metadata, currency, refs, "kucoin",
                                  (bitget or {}).get("match"), identity_cache)
    return {"status": "VERIFIED", "symbol": symbol, "identity": identity} if identity else \
        {"status": "UNVERIFIED", "symbol": symbol, "reason": "asset identity not independently verified"}


def add_optional_providers(snapshot: dict, latest: dict, market_summary: dict | None,
                           *, gate: MeteredClient | None = None, kucoin: MeteredClient | None = None,
                           uta: MeteredClient | None = None, identity_client: CoinGeckoClient | None = None,
                           enable_gate: bool = True, enable_kucoin: bool = True,
                           gate_catalog: list[dict] | None = None, kucoin_catalog: list[dict] | None = None,
                           gate_currencies: list[dict] | None = None,
                           kucoin_currencies: list[dict] | None = None,
                           references: list[dict] | None = None,
                           identity_cache: dict | None = None,
                           on_provider_start: Callable[[str], None] | None = None,
                           on_provider_finish: Callable[[str, dict], None] | None = None) -> dict:
    """Use only providers enabled by successful Actions probe; preserve Bitget rows."""
    gate = gate or MeteredClient(GATE_HOST)
    kucoin = kucoin or MeteredClient(KUCOIN_HOST)
    uta = uta or MeteredClient(KUCOIN_UTA)
    diagnostics = snapshot.setdefault("exchange_diagnostics", {})
    gate_catalog_at_ms = int(time.time() * 1000)
    kucoin_catalog_at_ms = int(time.time() * 1000)
    if enable_gate and gate_catalog is None:
        try:
            gate_catalog = gate.get("/futures/usdt/contracts")
            gate_catalog_at_ms = int(time.time() * 1000)
            if not isinstance(gate_catalog, list):
                raise ValueError("Gate catalog is not an array")
        except (DerivativesAPIError, ValueError) as exc:
            diagnostics["gate"] = getattr(exc, "details", {}) or {"reason": str(exc)[:180]}
            gate_catalog = None
    if enable_kucoin and kucoin_catalog is None:
        try:
            result = kucoin.get("/api/v1/contracts/active")
            kucoin_catalog = result.get("data")
            kucoin_catalog_at_ms = int(time.time() * 1000)
            if not isinstance(kucoin_catalog, list):
                raise ValueError("KuCoin catalog is not an array")
        except (DerivativesAPIError, ValueError) as exc:
            diagnostics["kucoin"] = getattr(exc, "details", {}) or {"reason": str(exc)[:180]}
            kucoin_catalog = None
    if enable_gate and gate_catalog is not None and gate_currencies is None:
        try:
            gate_currencies = gate.get("/spot/currencies")
            if not isinstance(gate_currencies, list):
                raise ValueError("Gate currency catalog invalid")
        except (DerivativesAPIError, ValueError) as exc:
            diagnostics["gate_identity"] = str(exc)[:180]
    if enable_kucoin and kucoin_catalog is not None and kucoin_currencies is None:
        try:
            result = uta.get("/api/v3/currencies")
            kucoin_currencies = result.get("data")
            if not isinstance(kucoin_currencies, list):
                raise ValueError("KuCoin currency catalog invalid")
        except (DerivativesAPIError, ValueError) as exc:
            diagnostics["kucoin_identity"] = str(exc)[:180]
    if references is None and (enable_gate or enable_kucoin):
        references = _references(identity_client, diagnostics)
    gate_by_name = {row.get("name"): row for row in gate_catalog or [] if isinstance(row, dict)}
    kucoin_by_symbol = {row.get("symbol"): row for row in kucoin_catalog or [] if isinstance(row, dict)}
    gate_coins = {row.get("currency"): row for row in gate_currencies or [] if isinstance(row, dict)}
    kucoin_coins = {row.get("currency"): row for row in kucoin_currencies or [] if isinstance(row, dict)}
    metadata = {row["market"]: row.get("metadata") or {} for row in latest["markets"]}
    eligible = eligible_markets(latest)
    counts = {"gate": defaultdict(int), "kucoin": defaultdict(int)}
    gate_targets = []
    for code, pair in snapshot["markets"].items():
        bitget = pair.get("bitget") or {}
        bitget_status = (bitget.get("match") or {}).get("status")
        reasons = ["PUBLIC_MARKET_COVERAGE"] if code in eligible else []
        asset = code.removeprefix("KRW-")
        if bitget_status in ("NO_MARKET", "ABSENT", "UNVERIFIED", "FETCH_FAILED"):
            reasons = ["BITGET_NO_VERIFIED_MARKET", *reasons]
        if f"{asset}_USDT" in gate_by_name and bitget_status == "VERIFIED":
            ratios = bitget.get("long_short") or {}
            if any((ratios.get(name) or {}).get("status") != "AVAILABLE"
                   for name in ("top_accounts", "top_positions")):
                reasons.append("MISSING_TOP_TRADER")
            if (bitget.get("liquidations") or {}).get("status") != "AVAILABLE":
                reasons.append("MISSING_LIQUIDATION_TOTALS")
            if (bitget.get("oi") or {}).get("value_usdt") is None:
                reasons.append("MISSING_USD_OI")
        if enable_gate and reasons and gate_catalog is not None:
            contract = gate_by_name.get(f"{asset}_USDT") or gate_by_name.get(f"1000{asset}_USDT")
            gate_targets.append((code, reasons, contract))
    gate_targets.sort(key=lambda item: (item[2] is None,
                                         "BITGET_NO_VERIFIED_MARKET" not in item[1], item[0]))
    gate_targets = _rotate_targets(gate_targets, GATE_LIMIT)
    def record_counts(provider: str, client_list: tuple, catalog: list | None,
                      targets: list[tuple], limit: int) -> None:
        rows = [pair[provider] for pair in snapshot["markets"].values() if provider in pair]
        observed = [row for row in rows if row["match"]["status"] == "VERIFIED"]
        missing = sum((row.get("oi") or {}).get("exchange_quantity") is None or
                      (row.get("funding") or {}).get("current_rate") is None for row in observed)
        stats = {**counts[provider], "catalog_count": len(catalog) if catalog is not None else None,
                 "eligible_count": len(targets), "selected_count": min(len(targets), limit),
                 "request_count": sum(client.http_attempt_count for client in client_list),
                 "api_operation_count": sum(client.request_count for client in client_list),
                 "oi_available": sum((row.get("oi") or {}).get("exchange_quantity") is not None for row in observed),
                 "funding_available": sum((row.get("funding") or {}).get("current_rate") is not None for row in observed),
                 "missing_core_count": missing,
                 "missing_core_rate": round(missing / len(observed), 4) if observed else None,
                 "duration_seconds": round(sum(client.request_duration_seconds for client in client_list), 2)}
        snapshot["summary"].setdefault("provider_counts", {})[provider] = stats

    if enable_gate and on_provider_start:
        on_provider_start("gate")
    for code, reasons, contract in gate_targets[:GATE_LIMIT]:
        pair = snapshot["markets"][code]
        asset = code.removeprefix("KRW-")
        match = _match_gate(code, metadata[code], contract, gate_coins.get(asset), references,
                            pair.get("bitget"), identity_cache)
        pair["gate"] = {"match": match, "selection_reasons": reasons,
                        **(_gate_data(gate, match["symbol"], contract, gate_catalog_at_ms)
                           if match["status"] == "VERIFIED"
                           else _unavailable_data())}
        counts["gate"][match["status"].lower()] += 1
    for code, reasons, contract in gate_targets[GATE_LIMIT:]:
        if contract is None:
            snapshot["markets"][code]["gate"] = {
                "match": {"status": "ABSENT", "symbol": None}, "selection_reasons": reasons,
                **_unavailable_data()}
            counts["gate"]["absent"] += 1
    if enable_gate and gate_catalog is None:
        for code, pair in snapshot["markets"].items():
            if code in eligible or (pair.get("bitget") or {}).get("match", {}).get("status") != "VERIFIED":
                pair["gate"] = {"match": {"status": "FETCH_FAILED", "symbol": None},
                                **_unavailable_data()}
                counts["gate"]["fetch_failed"] += 1
    if enable_gate:
        record_counts("gate", (gate,), gate_catalog, gate_targets, GATE_LIMIT)
        if on_provider_finish:
            on_provider_finish("gate", snapshot)
    kucoin_targets = []
    for code, pair in snapshot["markets"].items():
        reasons = ["PUBLIC_MARKET_COVERAGE"] if code in eligible else []
        bitget = pair.get("bitget") or {}
        gate_record = pair.get("gate") or {}
        if (bitget.get("match") or {}).get("status") != "VERIFIED" and \
                (gate_record.get("match") or {}).get("status") != "VERIFIED":
            reasons.insert(0, "NO_VERIFIED_MARKET")
        bitget_oi = bitget.get("oi") or {}
        gate_oi = gate_record.get("oi") or {}
        if (bitget.get("match") or {}).get("status") == "VERIFIED" and \
                any((bitget_oi.get("changes") or {}).get(f"{hours}h", {}).get("status") != "AVAILABLE"
                    for hours in (1, 4, 24)) and \
                any((gate_oi.get("changes") or {}).get(f"{hours}h", {}).get("status") != "AVAILABLE"
                    for hours in (1, 4, 24)):
            reasons.append("OI_HISTORY_GAP")
        if enable_kucoin and reasons and kucoin_catalog is not None:
            asset = code.removeprefix("KRW-")
            base = "XBT" if asset == "BTC" else asset
            contract = kucoin_by_symbol.get(f"{base}USDTM") or kucoin_by_symbol.get(f"1000{asset}USDTM")
            kucoin_targets.append((code, reasons, contract))
    kucoin_targets.sort(key=lambda item: (item[2] is None,
                                           "NO_VERIFIED_MARKET" not in item[1], item[0]))
    kucoin_targets = _rotate_targets(kucoin_targets, KUCOIN_LIMIT)
    if enable_kucoin and on_provider_start:
        on_provider_start("kucoin")
    for code, reasons, contract in kucoin_targets[:KUCOIN_LIMIT]:
        pair = snapshot["markets"][code]
        asset = code.removeprefix("KRW-")
        match = _match_kucoin(code, metadata[code], contract, kucoin_coins.get(asset), references,
                              pair.get("bitget"), identity_cache)
        pair["kucoin"] = {"match": match, "selection_reasons": reasons,
                          **(_kucoin_data(kucoin, uta, match["symbol"], contract, kucoin_catalog_at_ms)
                             if match["status"] == "VERIFIED" else _unavailable_data())}
        counts["kucoin"][match["status"].lower()] += 1
    for code, reasons, contract in kucoin_targets[KUCOIN_LIMIT:]:
        if contract is None:
            snapshot["markets"][code]["kucoin"] = {
                "match": {"status": "ABSENT", "symbol": None}, "selection_reasons": reasons,
                **_unavailable_data()}
            counts["kucoin"]["absent"] += 1
    if enable_kucoin and kucoin_catalog is None:
        for code, pair in snapshot["markets"].items():
            if code in eligible or (pair.get("bitget") or {}).get("match", {}).get("status") != "VERIFIED":
                pair["kucoin"] = {"match": {"status": "FETCH_FAILED", "symbol": None},
                                  **_unavailable_data()}
                counts["kucoin"]["fetch_failed"] += 1
    if enable_kucoin:
        record_counts("kucoin", (kucoin, uta), kucoin_catalog, kucoin_targets, KUCOIN_LIMIT)
        if on_provider_finish:
            on_provider_finish("kucoin", snapshot)
    snapshot["eligible_market_count"] = len(eligible)
    snapshot["optional_provider_policy"] = {"source": "official_public_api_only", "gate_cap": GATE_LIMIT,
                                            "kucoin_cap": KUCOIN_LIMIT,
                                            "no_cross_exchange_oi_aggregation": True}
    snapshot["summary"]["matched_market_count"] = sum(
        any((record.get("match") or {}).get("status") == "VERIFIED"
            for record in pair.values() if isinstance(record, dict))
        for pair in snapshot["markets"].values())
    return snapshot
