"""Live, read-only checks of official Gate and KuCoin public futures APIs."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from collector.main import write_json_atomic


GATE = "https://api.gateio.ws/api/v4"
KUCOIN_FUTURES = "https://api-futures.kucoin.com"
KUCOIN_UTA = "https://api.kucoin.com"
ASSETS = ("BTC", "ETH", "SOL")


def request(base: str, path: str, params: dict | None = None) -> tuple[object | None, dict]:
    url = base + path + ("?" + urlencode(params) if params else "")
    evidence = {"path": path, "http_status": None, "api_success": False,
                "status": "FETCH_FAILED", "error": None, "observed_at_ms": None}
    for attempt in range(3):
        try:
            with urlopen(Request(url, headers={"Accept": "application/json"}), timeout=12) as response:
                evidence["http_status"] = response.status
                raw = response.read()
                evidence["observed_at_ms"] = int(time.time() * 1000)
            payload = json.loads(raw)
            if base.startswith(KUCOIN_FUTURES) or base.startswith(KUCOIN_UTA):
                if not isinstance(payload, dict) or payload.get("code") != "200000":
                    raise ValueError(f"KuCoin API code: {payload.get('code') if isinstance(payload, dict) else None}")
            evidence["api_success"] = True
            evidence["status"] = "AVAILABLE"
            return payload, evidence
        except HTTPError as exc:
            evidence["http_status"] = exc.code
            evidence["error"] = exc.read(200).decode("utf-8", "replace")
            if exc.code not in (429, 500, 502, 503, 504):
                break
        except (URLError, TimeoutError, ValueError) as exc:
            evidence["error"] = str(exc)[:200]
        time.sleep(2 ** attempt)
    return None, evidence


def check(payload: object, evidence: dict, rows: object, fields: tuple[str, ...],
          time_key: str | None = None, max_age_seconds: int | None = None) -> dict:
    def time_value(row):
        try:
            return int(row.get(time_key) or 0)
        except (TypeError, ValueError):
            return 0
    sample = (max((row for row in rows if isinstance(row, dict)),
                  key=time_value, default=None)
              if isinstance(rows, list) and time_key else
              rows[0] if isinstance(rows, list) and rows else rows)
    if not isinstance(sample, dict):
        evidence["status"] = "FETCH_FAILED" if payload is not None else evidence["status"]
        evidence["error"] = evidence["error"] or "Missing or invalid response object"
        return evidence
    evidence["fields_present"] = {key: sample.get(key) is not None for key in fields}
    if not all(evidence["fields_present"].values()):
        evidence["status"] = "FETCH_FAILED"
        evidence["error"] = "Required response field missing"
    if time_key:
        raw_time = sample.get(time_key)
        try:
            parsed_time = int(raw_time) if raw_time is not None else None
        except (TypeError, ValueError):
            parsed_time = None
        evidence["data_at_ms"] = parsed_time * (1000 if parsed_time < 10**12 else 1) if parsed_time else None
        if evidence["data_at_ms"] is None:
            evidence["status"] = "FETCH_FAILED"
            evidence["error"] = "Data timestamp missing"
        else:
            evidence["age_seconds"] = round((evidence["observed_at_ms"] - evidence["data_at_ms"]) / 1000, 1)
            if max_age_seconds is not None and not (-300 <= evidence["age_seconds"] <= max_age_seconds):
                evidence["status"] = "FETCH_FAILED"
                evidence["error"] = "Data timestamp is stale or in the future"
    evidence["sample"] = {key: sample.get(key) for key in (*fields, *((time_key,) if time_key else ())) }
    return evidence


def probe_gate() -> dict:
    started = time.monotonic()
    output = {"host": GATE, "checks": {}, "samples": {}, "request_count": 0}
    def get(path, params=None):
        output["request_count"] += 1
        return request(GATE, path, params)
    payload, ev = get("/futures/usdt/contracts")
    rows = payload if isinstance(payload, list) else None
    output["checks"]["catalog"] = check(payload, ev, rows, ("name", "type", "quanto_multiplier", "status"))
    output["catalog_count"] = len(rows) if rows is not None else None
    for asset in ASSETS:
        symbol = f"{asset}_USDT"
        record = output["samples"][asset] = {"symbol": symbol}
        matched = next((row for row in rows or [] if row.get("name") == symbol), None)
        record["catalog_match"] = (
            {key: matched.get(key) for key in ("name", "type", "quanto_multiplier", "status", "mark_price", "position_size")}
            if matched else None)
        stats, ev = get("/futures/usdt/contract_stats", {"contract": symbol, "interval": "1h", "limit": 30})
        record["stats"] = check(stats, ev, stats, ("open_interest", "open_interest_usd", "lsr_account",
                                                   "top_lsr_account", "top_lsr_size", "long_liq_usd_new",
                                                   "short_liq_usd_new"), "time", 7200)
        record["stats"]["row_count"] = len(stats) if isinstance(stats, list) else None
        record["historical_oi"] = {"status": record["stats"]["status"],
                                    "timestamped_rows": sum(isinstance(r, dict) and r.get("time") is not None
                                                            and r.get("open_interest") is not None
                                                            for r in stats) if isinstance(stats, list) else 0}
        funding, ev = get("/futures/usdt/funding_rate", {"contract": symbol, "limit": 2})
        record["funding"] = check(funding, ev, funding, ("r",), "t", 86400)
    output["duration_seconds"] = round(time.monotonic() - started, 2)
    output["connection_success"] = output["checks"]["catalog"]["status"] == "AVAILABLE" and all(
        sample["catalog_match"] and sample["catalog_match"].get("position_size") is not None
        and sample["stats"]["status"] == "AVAILABLE" and sample["funding"]["status"] == "AVAILABLE"
        for sample in output["samples"].values())
    return output


def probe_kucoin() -> dict:
    started = time.monotonic()
    output = {"hosts": [KUCOIN_FUTURES, KUCOIN_UTA], "checks": {}, "samples": {}, "request_count": 0}
    def get(host, path, params=None):
        output["request_count"] += 1
        return request(host, path, params)
    payload, ev = get(KUCOIN_FUTURES, "/api/v1/contracts/active")
    rows = payload.get("data") if isinstance(payload, dict) else None
    output["checks"]["catalog"] = check(payload, ev, rows, ("symbol", "baseCurrency", "quoteCurrency", "multiplier"))
    output["catalog_count"] = len(rows) if isinstance(rows, list) else None
    for asset in ASSETS:
        base = "XBT" if asset == "BTC" else asset
        symbol = f"{base}USDTM"
        record = output["samples"][asset] = {"symbol": symbol}
        matched = next((row for row in rows or [] if row.get("symbol") == symbol), None)
        record["catalog_match"] = (
            {key: matched.get(key) for key in
             ("symbol", "baseCurrency", "quoteCurrency", "multiplier", "status", "openInterest")}
            if matched else None)
        current, ev = get(KUCOIN_UTA, "/api/ua/v2/market/open-interest", {"symbol": symbol})
        record["oi_current"] = check(current, ev, current.get("data") if isinstance(current, dict) else None,
                                     ("openInterest",), "ts", 7200)
        historical, ev = get(KUCOIN_UTA, "/api/ua/v2/market/open-interest",
                             {"symbol": symbol, "interval": "1hour", "pageSize": 30})
        historical_rows = historical.get("data") if isinstance(historical, dict) else None
        record["oi_history"] = check(historical, ev, historical_rows, ("openInterest",), "ts", 7200)
        record["oi_history"]["row_count"] = len(historical_rows) if isinstance(historical_rows, list) else None
        funding, ev = get(KUCOIN_FUTURES, f"/api/v1/funding-rate/{symbol}/current")
        record["funding"] = check(funding, ev, funding.get("data") if isinstance(funding, dict) else None,
                                  ("value",), "timePoint", 86400)
        now_ms = int(time.time() * 1000)
        funding_history, ev = get(KUCOIN_FUTURES, "/api/v1/contract/funding-rates",
                                  {"symbol": symbol, "from": now_ms - 3 * 86400_000, "to": now_ms})
        record["funding_history"] = check(
            funding_history, ev,
            funding_history.get("data") if isinstance(funding_history, dict) else None,
            ("fundingRate",), "timepoint", 3 * 86400)
        for name in ("general_long_short", "top_trader_accounts", "top_trader_positions", "liquidations"):
            record[name] = {"status": "NOT_SUPPORTED", "reason": "No documented public aggregate endpoint"}
    output["duration_seconds"] = round(time.monotonic() - started, 2)
    output["connection_success"] = output["checks"]["catalog"]["status"] == "AVAILABLE" and all(
        sample["catalog_match"] and sample["oi_current"]["status"] == "AVAILABLE" and sample["oi_history"]["status"] == "AVAILABLE"
        and sample["funding"]["status"] == "AVAILABLE" for sample in output["samples"].values())
    return output


def main() -> None:
    result = {"schema_version": "1.0", "checked_at_utc": datetime.now(UTC).isoformat(),
              "gate": probe_gate(), "kucoin": probe_kucoin()}
    path = Path("data/optional_provider_test.json")
    write_json_atomic(result, path, compact=True)
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    main()
