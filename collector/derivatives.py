"""Conservative public derivatives data collection for Upbit KRW assets."""

from __future__ import annotations

import json
import os
import re
import time
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener


KST = timezone(timedelta(hours=9), name="KST")
BINANCE_URL = "https://fapi.binance.com"
BYBIT_URL = "https://api.bybit.com"
HOURS_MS = 3_600_000

# Identity must be reviewed independently of a ticker match. The Upbit English
# name is checked on every run; exchange contract metadata is checked below.
# Add new assets only after reviewing their identity and any contract multiplier.
REVIEWED_IDENTITIES = {
    "BTC": "Bitcoin",
    "ETH": "Ethereum",
    "XRP": "XRP",
    "SOL": "Solana",
    "SHIB": "Shiba Inu",
}
ALLOWED_BASES = {"SHIB": {"SHIB": 1, "1000SHIB": 1000}}


class DerivativesAPIError(RuntimeError):
    """A public exchange request failed or returned an invalid response."""

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.details = details or {}


def _http_failure(base_url: str, path: str, exc: HTTPError) -> DerivativesAPIError:
    """Keep only bounded public diagnostics; never record request headers or keys."""
    host = urlsplit(base_url).hostname or base_url
    body = exc.read(512).decode("utf-8", errors="replace")
    try:
        parsed = json.loads(body)
        reason = str(parsed.get("msg") or parsed.get("retMsg") or "")
        exchange_code = parsed.get("code", parsed.get("retCode"))
    except (ValueError, AttributeError):
        reason = re.sub(r"<[^>]*>|\s+", " ", body).strip()
        exchange_code = None
    reason = re.sub(r"[\x00-\x1f\x7f]", " ", reason)[:160]
    lower = reason.lower()
    if exc.code == 451:
        category = "LEGAL_RESTRICTION"
    elif exc.code in (418, 429) or "too frequent" in lower or "rate limit" in lower:
        category = "RATE_LIMIT"
    elif exc.code == 403 and any(
        marker in lower for marker in ("region", "restricted", "country", "geoblock")
    ):
        category = "REGION_RESTRICTION"
    elif exc.code == 403:
        category = "FORBIDDEN_UNDETERMINED"
    elif exc.code >= 500:
        category = "SERVER_ERROR"
    else:
        category = "HTTP_ERROR"
    headers = exc.headers or {}
    details = {"host": host, "path": path, "http_status": exc.code,
               "category": category, "exchange_code": exchange_code,
               "response_message": reason or None,
               "retry_after": headers.get("Retry-After"),
               "request_id": headers.get("X-MBX-UUID") or headers.get("X-Bapi-Trace-Id")}
    return DerivativesAPIError(
        f"{host}{path}: HTTP {exc.code} ({category})"
        + (f"; {reason}" if reason else ""), details=details)


class PublicClient:
    def __init__(
        self, base_url: str, *, opener: Any | None = None, attempts: int = 3,
        timeout: float = 10, interval: float = 0.12, sleeper=time.sleep,
    ) -> None:
        self.base_url = base_url
        self.opener = opener or build_opener()
        self.attempts = attempts
        self.timeout = timeout
        self.interval = interval
        self.sleep = sleeper

    def get(self, path: str, params: dict[str, Any] | None = None,
            headers: dict[str, str] | None = None) -> Any:
        url = self.base_url + path + ("?" + urlencode(params) if params else "")
        for attempt in range(self.attempts):
            try:
                request = Request(url, headers={"Accept": "application/json", **(headers or {})})
                if hasattr(self, "http_attempt_count"):
                    self.http_attempt_count += 1
                with self.opener.open(request, timeout=self.timeout) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                if isinstance(payload, dict) and "retCode" in payload:
                    if payload["retCode"] != 0:
                        host = urlsplit(self.base_url).hostname or self.base_url
                        message = str(payload.get("retMsg", ""))[:160]
                        raise DerivativesAPIError(
                            f"{host}{path}: exchange retCode {payload['retCode']}: {message}",
                            details={"host": host, "path": path,
                                     "category": "EXCHANGE_ERROR",
                                     "exchange_code": payload["retCode"],
                                     "response_message": message}
                        )
                self.sleep(self.interval)
                return payload
            except HTTPError as exc:
                if exc.code not in (418, 429, 500, 502, 503, 504) or attempt + 1 == self.attempts:
                    raise _http_failure(self.base_url, path, exc) from exc
            except (URLError, TimeoutError, ValueError) as exc:
                if attempt + 1 == self.attempts:
                    host = urlsplit(self.base_url).hostname or self.base_url
                    raise DerivativesAPIError(
                        f"{host}{path}: {exc}",
                        details={"host": host, "path": path,
                                 "category": "NETWORK_OR_RESPONSE_ERROR",
                                 "message": str(exc)[:160]}) from exc
            except DerivativesAPIError:
                if attempt + 1 == self.attempts:
                    raise
            self.sleep(min(8, 2 ** attempt))
        raise AssertionError("unreachable")


def _number(value: Any) -> float | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return float(number) if number.is_finite() else None


def _timestamp(value: Any) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _normal_name(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _contract_address(value: Any) -> str | None:
    """EVM hex addresses ignore case; base58/Solana addresses do not."""
    if not isinstance(value, str):
        return None
    address = value.strip()
    if re.fullmatch(r"0x(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})", address):
        return address.lower()
    if re.fullmatch(r"0x[0-9a-fA-F]{1,64}(?:::[A-Za-z][A-Za-z0-9_]{0,63}){2}", address):
        return address.lower()
    if re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}", address):
        return address
    return None


def _allowed_bases(code: str) -> dict[str, int]:
    return ALLOWED_BASES.get(code, {code: 1})


def _catalog(client: PublicClient, exchange: str) -> list[dict[str, Any]]:
    if exchange == "binance":
        result = client.get("/fapi/v1/exchangeInfo")
        rows = result.get("symbols") if isinstance(result, dict) else None
        if not isinstance(rows, list):
            raise DerivativesAPIError("Binance exchangeInfo has no symbol list")
        return rows
    rows = []
    cursor = None
    seen = set()
    while True:
        params = {"category": "linear", "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        result = client.get("/v5/market/instruments-info", params)
        page = result.get("result", {}) if isinstance(result, dict) else {}
        batch = page.get("list")
        if not isinstance(batch, list):
            raise DerivativesAPIError("Bybit instruments-info has no symbol list")
        rows.extend(batch)
        next_cursor = page.get("nextPageCursor")
        if not next_cursor:
            return rows
        if next_cursor in seen:
            raise DerivativesAPIError("Bybit catalog repeated its page cursor")
        seen.add(next_cursor)
        cursor = next_cursor


def match_contract(
    code: str, upbit_metadata: dict[str, Any], instruments: list[dict[str, Any]],
    exchange: str,
) -> dict[str, Any]:
    """Require reviewed identity plus live exchange contract metadata."""
    asset = code.removeprefix("KRW-")
    bases = _allowed_bases(asset)
    discoverable_bases = set(bases) | {f"1000{asset}"}
    candidates = [row for row in instruments if row.get("symbol") in
                  {base + "USDT" for base in discoverable_bases}]
    if not candidates:
        return {"status": "ABSENT", "symbol": None, "base_units_per_exchange_unit": None}
    if len(candidates) != 1:
        return {"status": "UNVERIFIED", "symbol": None,
                "reason": "multiple candidate contracts", "base_units_per_exchange_unit": None}
    row = candidates[0]
    symbol = row["symbol"]
    base = row.get("baseAsset") if exchange == "binance" else row.get("baseCoin")
    factor = bases.get(base)
    if exchange == "binance":
        metadata_ok = (
            row.get("status") == "TRADING"
            and row.get("contractType") == "PERPETUAL"
            and row.get("quoteAsset") == "USDT"
            and row.get("marginAsset") == "USDT"
            and row.get("underlyingType") == "COIN"
        )
    else:
        metadata_ok = (
            row.get("status") == "Trading"
            and row.get("contractType") == "LinearPerpetual"
            and row.get("quoteCoin") == "USDT"
            and row.get("settleCoin") == "USDT"
            and row.get("isPreListing") is False
        )
    if factor is None or not metadata_ok or symbol != f"{base}USDT":
        return {"status": "UNVERIFIED", "symbol": symbol,
                "reason": "contract metadata or unit is unverified",
                "base_units_per_exchange_unit": None}
    reviewed_name = REVIEWED_IDENTITIES.get(asset)
    if not reviewed_name or _normal_name(upbit_metadata.get("english_name")) != _normal_name(reviewed_name):
        return {"status": "UNVERIFIED", "symbol": symbol,
                "reason": "underlying asset identity is unverified",
                "base_units_per_exchange_unit": None}
    return {"status": "VERIFIED", "symbol": symbol, "base_asset": base,
            "base_units_per_exchange_unit": factor}


def _oi_changes(current: float | None, current_at: int | None,
                history: list[dict[str, Any]], key: str) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    for hours in (1, 4, 24):
        target = current_at - hours * HOURS_MS if current_at else None
        candidates = [row for row in history if target is not None
                      and (ts := _timestamp(row.get("timestamp"))) is not None
                      and target - HOURS_MS <= ts <= target]
        reference = max(candidates, key=lambda row: _timestamp(row["timestamp"])) if candidates else None
        old = _number(reference.get(key)) if reference else None
        changes[f"{hours}h"] = {
            "pct": round((current / old - 1) * 100, 8)
            if current is not None and old not in (None, 0) else None,
            "reference_at_ms": _timestamp(reference.get("timestamp")) if reference else None,
        }
    return changes


def _funding(rows: list[dict[str, Any]], rate_key: str, time_key: str) -> dict[str, Any]:
    valid = sorted((row for row in rows if _timestamp(row.get(time_key)) is not None),
                   key=lambda row: _timestamp(row[time_key]), reverse=True)
    current = _number(valid[0].get(rate_key)) if valid else None
    previous = _number(valid[1].get(rate_key)) if len(valid) > 1 else None
    direction = None
    if current is not None and previous is not None:
        direction = "UP" if current > previous else "DOWN" if current < previous else "UNCHANGED"
    return {"latest_rate": current, "previous_rate": previous,
            "direction": direction,
            "latest_at_ms": _timestamp(valid[0][time_key]) if valid else None,
            "previous_at_ms": _timestamp(valid[1][time_key]) if len(valid) > 1 else None}


def _ratio(row: dict[str, Any] | None, value_key: str,
           long_key: str, short_key: str) -> dict[str, Any]:
    if not row:
        return {"ratio": None, "at_ms": None}
    ratio = _number(row.get(value_key)) if value_key else None
    if ratio is None:
        long = _number(row.get(long_key))
        short = _number(row.get(short_key))
        ratio = round(long / short, 8) if long is not None and short not in (None, 0) else None
    return {"ratio": ratio, "at_ms": _timestamp(row.get("timestamp"))}


def _liquidations() -> dict[str, Any]:
    # REST has no complete historical, symbol-level liquidation series. A
    # scheduled run cannot reconstruct prior websocket events.
    return {"status": "INSUFFICIENT_HISTORY", "scope": "symbol",
            "1h": {"long": None, "short": None},
            "4h": {"long": None, "short": None},
            "24h": {"long": None, "short": None}}


def _empty_data() -> dict[str, Any]:
    return {
        "oi": {"exchange_quantity": None, "exchange_quantity_unit": None,
               "contract_quantity": None, "base_asset_quantity": None,
               "value_usdt": None, "value_at_ms": None, "at_ms": None,
               "changes": {f"{h}h": {"pct": None, "reference_at_ms": None}
                           for h in (1, 4, 24)}},
        "funding": _funding([], "fundingRate", "fundingTime"),
        "long_short": {name: {"ratio": None, "at_ms": None, "status": "NOT_SUPPORTED"}
                       for name in ("general_accounts", "top_accounts", "top_positions")},
        "liquidations": _liquidations(),
        "errors": [],
    }


def _unavailable_data() -> dict[str, Any]:
    """Compact nulls for markets without a verified, queryable contract."""
    return {"oi": None, "funding": None, "long_short": None,
            "liquidations": None, "errors": []}


def _try(data: dict[str, Any], stage: str, operation) -> Any:
    try:
        return operation()
    except (DerivativesAPIError, KeyError, TypeError, ValueError) as exc:
        data["errors"].append({"stage": stage, "reason": str(exc)[:180]})
        return None


def _binance_data(client: PublicClient, symbol: str, data: dict[str, Any],
                  api_key: str | None) -> None:
    current = _try(data, "oi_current", lambda: client.get("/fapi/v1/openInterest", {"symbol": symbol}))
    history = _try(data, "oi_history", lambda: client.get(
        "/futures/data/openInterestHist", {"symbol": symbol, "period": "1h", "limit": 30}))
    oi = data["oi"]
    if isinstance(current, dict):
        oi["exchange_quantity"] = _number(current.get("openInterest"))
        oi["exchange_quantity_unit"] = "UNCONFIRMED"
        oi["at_ms"] = _timestamp(current.get("time"))
    if isinstance(history, list):
        oi["changes"] = _oi_changes(oi["exchange_quantity"], oi["at_ms"], history, "sumOpenInterest")
        if history:
            newest = max(history, key=lambda row: _timestamp(row.get("timestamp")) or 0)
            oi["value_usdt"] = _number(newest.get("sumOpenInterestValue"))
            oi["value_at_ms"] = _timestamp(newest.get("timestamp"))
    funding = _try(data, "funding", lambda: client.get("/fapi/v1/fundingRate",
                                                  {"symbol": symbol, "limit": 2}))
    if isinstance(funding, list):
        data["funding"] = _funding(funding, "fundingRate", "fundingTime")
    general = _try(data, "general_accounts", lambda: client.get(
        "/futures/data/globalLongShortAccountRatio",
        {"symbol": symbol, "period": "1h", "limit": 1}))
    if isinstance(general, list):
        data["long_short"]["general_accounts"] = {
            **_ratio(general[-1] if general else None, "longShortRatio", "longAccount", "shortAccount"),
            "status": "AVAILABLE" if general else "NO_DATA"}
    else:
        data["long_short"]["general_accounts"]["status"] = "FETCH_FAILED"
    if not api_key:
        return
    for name, endpoint in (("top_accounts", "topLongShortAccountRatio"),
                           ("top_positions", "topLongShortPositionRatio")):
        rows = _try(data, name, lambda endpoint=endpoint: client.get(
            f"/futures/data/{endpoint}", {"symbol": symbol, "period": "1h", "limit": 1},
            {"X-MBX-APIKEY": api_key}))
        if isinstance(rows, list):
            data["long_short"][name] = {
                **_ratio(rows[-1] if rows else None, "longShortRatio", "longAccount", "shortAccount"),
                "status": "AVAILABLE" if rows else "NO_DATA"}
        else:
            data["long_short"][name]["status"] = "FETCH_FAILED"


def _bybit_data(client: PublicClient, symbol: str, factor: int,
                ticker: dict[str, Any] | None, ticker_at: int | None,
                data: dict[str, Any]) -> None:
    oi = data["oi"]
    if ticker:
        oi["exchange_quantity"] = _number(ticker.get("openInterest"))
        oi["exchange_quantity_unit"] = symbol.removesuffix("USDT")
        oi["base_asset_quantity"] = (
            round(oi["exchange_quantity"] * factor, 8)
            if oi["exchange_quantity"] is not None else None
        )
        oi["value_usdt"] = _number(ticker.get("openInterestValue"))
        oi["value_at_ms"] = ticker_at if oi["value_usdt"] is not None else None
        oi["at_ms"] = ticker_at
    else:
        data["errors"].append({"stage": "oi_current", "reason": "ticker unavailable"})
    history = _try(data, "oi_history", lambda: client.get(
        "/v5/market/open-interest",
        {"category": "linear", "symbol": symbol, "intervalTime": "1h", "limit": 30}))
    if isinstance(history, dict):
        rows = history.get("result", {}).get("list", [])
        oi["changes"] = _oi_changes(oi["exchange_quantity"], oi["at_ms"], rows, "openInterest")
    funding = _try(data, "funding", lambda: client.get(
        "/v5/market/funding/history", {"category": "linear", "symbol": symbol, "limit": 2}))
    if isinstance(funding, dict):
        data["funding"] = _funding(funding.get("result", {}).get("list", []),
                                   "fundingRate", "fundingRateTimestamp")
    general = _try(data, "general_accounts", lambda: client.get(
        "/v5/market/account-ratio",
        {"category": "linear", "symbol": symbol, "period": "1h", "limit": 1}))
    if isinstance(general, dict):
        rows = general.get("result", {}).get("list", [])
        data["long_short"]["general_accounts"] = {
            **_ratio(rows[0] if rows else None, "", "buyRatio", "sellRatio"),
            "status": "AVAILABLE" if rows else "NO_DATA"}
    else:
        data["long_short"]["general_accounts"]["status"] = "FETCH_FAILED"


def build_derivatives_snapshot(
    latest: dict[str, Any], binance: PublicClient, bybit: PublicClient,
    *, api_key: str | None = None, now: datetime | None = None,
) -> dict[str, Any]:
    """Collect each verified contract independently; retain per-market errors."""
    records = latest.get("markets")
    if not isinstance(records, list) or not records:
        raise ValueError("latest snapshot has no KRW markets")
    krw = {row["market"]: row.get("metadata") or {} for row in records
           if str(row.get("market", "")).startswith("KRW-")}
    if not krw or len(krw) != len(records):
        raise ValueError("latest snapshot has invalid or duplicate markets")
    catalogs = {}
    catalog_errors = {}
    exchange_diagnostics = {}
    for name, client in (("binance", binance), ("bybit", bybit)):
        try:
            catalogs[name] = _catalog(client, name)
        except DerivativesAPIError as exc:
            catalogs[name] = None
            catalog_errors[name] = str(exc)[:180]
            exchange_diagnostics[name] = exc.details or {"category": "API_ERROR", "message": str(exc)[:180]}
    ticker_by_symbol: dict[str, dict[str, Any]] = {}
    ticker_at = None
    if catalogs["bybit"] is not None:
        try:
            payload = bybit.get("/v5/market/tickers", {"category": "linear"})
            ticker_at = _timestamp(payload.get("time"))
            ticker_by_symbol = {row["symbol"]: row for row in payload["result"]["list"]}
        except (DerivativesAPIError, KeyError, TypeError) as exc:
            catalog_errors["bybit_tickers"] = str(exc)[:180]

    markets = {}
    for code, metadata in sorted(krw.items()):
        exchange_results = {}
        for name, client in (("binance", binance), ("bybit", bybit)):
            if catalogs[name] is None:
                exchange_results[name] = {"match": {"status": "FETCH_FAILED", "symbol": None},
                                          **_unavailable_data()}
                continue
            match = match_contract(code, metadata, catalogs[name], name)
            result = {"match": match,
                      **(_empty_data() if match["status"] == "VERIFIED" else _unavailable_data())}
            if match["status"] == "VERIFIED":
                if name == "binance":
                    _binance_data(client, match["symbol"], result, api_key)
                else:
                    _bybit_data(client, match["symbol"], match["base_units_per_exchange_unit"],
                                ticker_by_symbol.get(match["symbol"]), ticker_at, result)
            exchange_results[name] = result
        markets[code] = exchange_results

    instant = (now or datetime.now(UTC)).astimezone(UTC)
    return {
        "schema_version": "3.0",
        "collected_at": {"utc": instant.isoformat().replace("+00:00", "Z"),
                         "kst": instant.astimezone(KST).isoformat()},
        "upbit_source_collected_at": latest.get("collected_at"),
        "summary": {
            "market_count": len(markets),
            "matched_market_count": sum(any(v["match"]["status"] == "VERIFIED"
                                            for v in pair.values()) for pair in markets.values()),
            "absent_market_count": sum(all(v["match"]["status"] == "ABSENT"
                                           for v in pair.values()) for pair in markets.values()),
            "failed_market_count": sum(any(v["match"]["status"] == "FETCH_FAILED" or v["errors"]
                                           for v in pair.values()) for pair in markets.values()),
            "unverified_market_count": sum(any(v["match"]["status"] == "UNVERIFIED"
                                              for v in pair.values()) for pair in markets.values()),
        },
        "catalog_errors": catalog_errors,
        "exchange_diagnostics": exchange_diagnostics,
        "markets": markets,
    }


def default_clients() -> tuple[PublicClient, PublicClient]:
    return PublicClient(BINANCE_URL), PublicClient(BYBIT_URL)
