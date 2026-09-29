"""Verified Bitget USDT perpetual data for the existing KRW derivatives snapshot."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

from collector.derivatives import (
    ALLOWED_BASES, REVIEWED_IDENTITIES, DerivativesAPIError, PublicClient,
    _empty_data, _funding, _normal_name, _number, _timestamp, _unavailable_data,
)
from collector.bitget_identity import CoinGeckoClient, resolve_identities


BITGET_URL = "https://api.bitget.com"
CATEGORY = "USDT-FUTURES"


class BitgetClient(PublicClient):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(BITGET_URL, interval=0.25, **kwargs)
        self.http_attempt_count = 0
        self.api_operation_count = 0
        self.request_duration_seconds = 0.0

    def get(self, path: str, params: dict | None = None, headers: dict | None = None) -> Any:
        self.api_operation_count += 1
        started = time.monotonic()
        try:
            payload = super().get(path, params, headers)
        finally:
            self.request_duration_seconds += time.monotonic() - started
        if not isinstance(payload, dict) or str(payload.get("code")) != "00000":
            code = payload.get("code") if isinstance(payload, dict) else None
            message = str(payload.get("msg", ""))[:160] if isinstance(payload, dict) else "Invalid response"
            raise DerivativesAPIError(
                f"api.bitget.com{path}: API {code}: {message}",
                details={"host": "api.bitget.com", "path": path, "category": "EXCHANGE_ERROR",
                         "exchange_code": code, "response_message": message})
        return payload


def match_bitget(code: str, metadata: dict, instruments: list[dict],
                 identities: dict[str, dict] | None = None) -> dict:
    """A matching ticker alone never verifies token identity or contract units."""
    asset = code.removeprefix("KRW-")
    bases = ALLOWED_BASES.get(asset, {asset: 1})
    symbols = {f"{base}USDT" for base in set(bases) | {f"1000{asset}"}}
    rows = [row for row in instruments if row.get("symbol") in symbols]
    if not rows:
        return {"status": "NO_MARKET", "symbol": None}
    if len(rows) != 1:
        return {"status": "UNVERIFIED", "symbol": None, "reason": "multiple candidate contracts"}
    row = rows[0]
    base = row.get("baseCoin")
    valid = (row.get("category") == CATEGORY and row.get("quoteCoin") == "USDT"
             and row.get("type") == "perpetual" and row.get("status") == "online"
             and row.get("symbolType") == "crypto" and base in bases
             and row.get("symbol") == f"{base}USDT")
    if not valid:
        return {"status": "UNVERIFIED", "symbol": row.get("symbol"),
                "reason": "contract metadata or multiplier is unverified"}
    identity = (identities or {}).get(code)
    if identity is not None and identity.get("upbit_name") != _normal_name(metadata.get("english_name")):
        identity = None
    if identity is None:
        name = REVIEWED_IDENTITIES.get(asset)
        if name and _normal_name(metadata.get("english_name")) == _normal_name(name):
            identity = {"method": "REVIEWED"}
    if identity is None:
        return {"status": "UNVERIFIED", "symbol": row["symbol"],
                "reason": "underlying asset identity is unverified"}
    return {"status": "VERIFIED", "symbol": row["symbol"], "base_asset": base,
            "base_units_per_exchange_unit": bases[base],
            "identity": identity}


def _get(result: dict, stage: str, client: BitgetClient, path: str, params: dict) -> Any:
    try:
        return client.get(path, params).get("data")
    except (DerivativesAPIError, KeyError, TypeError, ValueError) as exc:
        result["errors"].append({"stage": stage, "reason": str(exc)[:180]})
        return None


def _latest_ratio(rows: Any, value_key: str) -> dict:
    if not isinstance(rows, list) or not rows:
        return {"ratio": None, "at_ms": None,
                "status": "NO_DATA" if isinstance(rows, list) else "FETCH_FAILED"}
    valid = [row for row in rows if isinstance(row, dict) and _timestamp(row.get("ts"))]
    if not valid:
        return {"ratio": None, "at_ms": None, "status": "NO_DATA"}
    row = max(valid, key=lambda item: _timestamp(item["ts"]))
    ratio = _number(row.get(value_key))
    return {"ratio": ratio, "at_ms": _timestamp(row["ts"]),
            "status": "AVAILABLE" if ratio is not None else "NO_DATA"}


def _collect_one(client: BitgetClient, symbol: str, sleeper, factor: int = 1) -> dict:
    result = _empty_data()
    oi = result["oi"]
    result["funding"].update({"current_rate": None, "next_at_ms": None})
    for hours in (1, 4, 24):
        oi["changes"][f"{hours}h"]["status"] = "INSUFFICIENT_HISTORY"
    oi["history_status"] = "NOT_SUPPORTED"  # No documented historical OI endpoint.
    current = _get(result, "oi_current", client, "/api/v3/market/open-interest",
                   {"category": CATEGORY, "symbol": symbol})
    oi["status"] = "FETCH_FAILED" if current is None else "NO_DATA"
    if isinstance(current, dict):
        rows = current.get("list")
        matching = [row for row in rows if row.get("symbol") == symbol] if isinstance(rows, list) else []
        if len(matching) == 1:
            oi["exchange_quantity"] = _number(matching[0].get("openInterest"))
            oi["exchange_quantity_unit"] = "UNCONFIRMED"
            oi["at_ms"] = _timestamp(current.get("ts"))
            oi["status"] = "AVAILABLE" if oi["exchange_quantity"] is not None else "NO_DATA"
            # The v3 OI field has no stated unit. The official v2 ticker calls
            # holdingAmount a coin quantity. Confirm that both live quantities
            # agree before applying that documented unit to v3 OI.
            if oi["exchange_quantity"] is not None and factor == 1:
                ticker_rows = _get(result, "oi_unit_reference", client,
                                   "/api/v2/mix/market/ticker",
                                   {"productType": CATEGORY, "symbol": symbol})
                matching_tickers = [row for row in ticker_rows if row.get("symbol") == symbol] \
                    if isinstance(ticker_rows, list) else []
                unit_ticker = matching_tickers[0] if len(matching_tickers) == 1 else None
            else:
                unit_ticker = None
            if unit_ticker:
                reference = _number(unit_ticker.get("holdingAmount"))
                mark = _number(unit_ticker.get("markPrice"))
                ticker_at = _timestamp(unit_ticker.get("ts"))
                current_at = oi["at_ms"]
                if (reference is not None and reference > 0 and mark is not None and mark > 0
                        and ticker_at is not None and current_at is not None
                        and abs(ticker_at - current_at) <= 120_000
                        and abs(reference - oi["exchange_quantity"]) / reference <= 0.01):
                    oi["exchange_quantity_unit"] = symbol.removesuffix("USDT")
                    oi["base_asset_quantity"] = oi["exchange_quantity"]
                    oi["value_usdt"] = round(oi["exchange_quantity"] * mark, 8)
                    oi["value_at_ms"] = ticker_at
                    oi["unit_verification"] = "V2_HOLDING_AMOUNT_CROSSCHECK"
                else:
                    oi["unit_verification"] = "UNCONFIRMED_V2_MISMATCH"
        else:
            result["errors"].append({"stage": "oi_current", "reason": "symbol missing or ambiguous"})
    try:
        funding_payload = client.get("/api/v3/market/current-fund-rate",
                                     {"category": CATEGORY, "symbol": symbol})
        current_funding = funding_payload.get("data")
        result["funding"]["current_observed_at_ms"] = _timestamp(funding_payload.get("requestTime"))
    except (DerivativesAPIError, KeyError, TypeError, ValueError) as exc:
        result["errors"].append({"stage": "funding_current", "reason": str(exc)[:180]})
        current_funding = None
    result["funding"]["current_status"] = "FETCH_FAILED" if current_funding is None else "NO_DATA"
    if isinstance(current_funding, list):
        matching = [row for row in current_funding if row.get("symbol") == symbol]
        if len(matching) == 1:
            result["funding"]["current_rate"] = _number(matching[0].get("fundingRate"))
            result["funding"]["next_at_ms"] = _timestamp(matching[0].get("nextUpdate"))
            if result["funding"]["current_rate"] is not None:
                result["funding"]["current_status"] = "AVAILABLE"
    history = _get(result, "funding_history", client, "/api/v3/market/history-fund-rate",
                   {"category": CATEGORY, "symbol": symbol, "limit": 2})
    result["funding"]["history_status"] = "FETCH_FAILED" if history is None else "NO_DATA"
    if isinstance(history, dict) and isinstance(history.get("resultList"), list):
        settled = _funding(history["resultList"], "fundingRate", "fundingRateTimestamp")
        result["funding"].update(settled)
        if settled["latest_rate"] is not None:
            result["funding"]["history_status"] = "AVAILABLE"
    for name, path, key in (
        ("general_accounts", "/api/v3/market/futures-long-short", "longShortRatio"),
        ("active_accounts", "/api/v3/market/futures-account-long-short", "longShortAccountRatio"),
        ("active_positions", "/api/v3/market/futures-position-long-short", "longShortPositionRatio"),
    ):
        rows = _get(result, name, client, path, {"symbol": symbol, "period": "1h"})
        result["long_short"][name] = _latest_ratio(rows, key)
        sleeper(1.05)  # Bitget trading-data endpoints: 1 request/second/IP.
    # These active-account/position series are not documented as Top Trader.
    result["long_short"]["top_accounts"]["status"] = "NOT_SUPPORTED"
    result["long_short"]["top_positions"]["status"] = "NOT_SUPPORTED"
    liq = _get(result, "liquidations", client, "/api/v3/market/liquidations",
               {"category": CATEGORY, "symbol": symbol, "limit": 100})
    if isinstance(liq, dict) and isinstance(liq.get("list"), list):
        rows = [row for row in liq["list"] if row.get("symbol") == symbol]
        times = [_timestamp(row.get("ts")) for row in rows]
        valid_times = [value for value in times if value is not None]
        result["liquidations"].update({
            "status": "INSUFFICIENT_HISTORY",
            "sampled_event_count": len(rows),
            "sampled_buy_count": sum(row.get("side") == "buy" for row in rows),
            "sampled_sell_count": sum(row.get("side") == "sell" for row in rows),
            "sampled_newest_at_ms": max(valid_times, default=None),
            "sampled_oldest_at_ms": min(valid_times, default=None),
            "direction_mapping": "UNVERIFIED",
            "reason": "First page is incomplete for 1h/4h/24h totals; buy/sell is not labeled long/short",
        })
    elif liq is None:
        result["liquidations"]["status"] = "FETCH_FAILED"
    return result


def add_bitget(snapshot: dict, latest: dict, client: BitgetClient, *, sleeper=None,
               identity_client: CoinGeckoClient | None = None,
               bitget_coins: list[dict] | None = None,
               references: list[dict] | None = None,
               identity_cache: dict | None = None) -> dict:
    """Append a separate provider record for every Upbit KRW market."""
    sleeper = sleeper or __import__("time").sleep
    try:
        payload = client.get("/api/v3/market/instruments", {"category": CATEGORY})
        instruments = payload.get("data")
        if not isinstance(instruments, list):
            raise DerivativesAPIError("Bitget instrument response has no list")
    except DerivativesAPIError as exc:
        instruments = None
        snapshot["catalog_errors"]["bitget"] = str(exc)[:180]
        snapshot["exchange_diagnostics"]["bitget"] = exc.details or {
            "category": "INVALID_RESPONSE", "message": str(exc)[:180]}
    if bitget_coins is None:
        try:
            coins_payload = client.get("/api/v2/spot/public/coins")
            bitget_coins = coins_payload.get("data")
            if not isinstance(bitget_coins, list):
                raise DerivativesAPIError("Bitget coin catalog has no list")
        except DerivativesAPIError as exc:
            snapshot["exchange_diagnostics"]["bitget_coin_identity"] = (
                exc.details or {"category": "INVALID_RESPONSE", "message": str(exc)[:180]})
    if references is None:
        try:
            references = (identity_client or CoinGeckoClient()).get(
                "/api/v3/coins/list", {"include_platform": "true"})
            if not isinstance(references, list):
                raise DerivativesAPIError("CoinGecko identity catalog has no list")
        except DerivativesAPIError as exc:
            snapshot["exchange_diagnostics"]["coingecko_identity"] = (
                exc.details or {"category": "INVALID_RESPONSE", "message": str(exc)[:180]})
    identities, identity_summary = resolve_identities(
        latest, bitget_coins, references, identity_cache)
    snapshot["identity_validation"] = identity_summary
    metadata = {row["market"]: row.get("metadata") or {} for row in latest["markets"]}
    for code, pair in snapshot["markets"].items():
        if instruments is None:
            pair["bitget"] = {"match": {"status": "FETCH_FAILED", "symbol": None},
                              **_unavailable_data()}
            continue
        match = match_bitget(code, metadata[code], instruments, identities)
        pair["bitget"] = {"match": match,
                          **(_collect_one(client, match["symbol"], sleeper,
                                          match["base_units_per_exchange_unit"])
                             if match["status"] == "VERIFIED" else _unavailable_data())}
    summary = snapshot["summary"]
    summary["matched_market_count"] = sum(any(row["match"]["status"] == "VERIFIED"
                                              for row in pair.values())
                                          for pair in snapshot["markets"].values())
    summary["absent_market_count"] = sum(all(row["match"]["status"] in ("ABSENT", "NO_MARKET")
                                            for row in pair.values())
                                         for pair in snapshot["markets"].values())
    summary["failed_market_count"] = sum(any(row["match"]["status"] == "FETCH_FAILED"
                                            or row["errors"] for row in pair.values())
                                         for pair in snapshot["markets"].values())
    summary["unverified_market_count"] = sum(any(row["match"]["status"] == "UNVERIFIED"
                                                for row in pair.values())
                                             for pair in snapshot["markets"].values())
    bitget_rows = [pair["bitget"] for pair in snapshot["markets"].values()]
    verified_rows = [row for row in bitget_rows if row["match"]["status"] == "VERIFIED"]
    missing_core = sum((row["oi"] or {}).get("exchange_quantity") is None or
                       (row["funding"] or {}).get("current_rate") is None for row in verified_rows)
    summary["provider_counts"] = {"bitget": {
        **{status.lower(): sum(row["match"]["status"] == status for row in bitget_rows)
           for status in ("VERIFIED", "NO_MARKET", "UNVERIFIED", "FETCH_FAILED")},
        "oi_available": sum((row["oi"] or {}).get("exchange_quantity") is not None
                            for row in bitget_rows),
        "funding_available": sum((row["funding"] or {}).get("current_rate") is not None
                                 for row in bitget_rows),
        "data_fetch_failed": sum(bool(row["errors"]) for row in bitget_rows),
        "request_count": getattr(client, "http_attempt_count", None),
        "api_operation_count": getattr(client, "api_operation_count", None),
        "duration_seconds": round(getattr(client, "request_duration_seconds", 0), 2),
        "missing_core_count": missing_core,
        "missing_core_rate": round(missing_core / len(verified_rows), 4) if verified_rows else None,
    }}
    instant = datetime.now(UTC)
    snapshot["collected_at"] = {"utc": instant.isoformat().replace("+00:00", "Z"),
                                "kst": instant.astimezone(timezone(timedelta(hours=9))).isoformat()}
    return snapshot
