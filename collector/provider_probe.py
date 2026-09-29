"""Audit free, official OKX and Bitget derivatives APIs from the Actions runner.

This is a connectivity and response-shape probe, not a production collector.
No authentication, proxy, symbol inference, or fabricated observations are used.
"""

from __future__ import annotations

import argparse
import json
import re
import time
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, build_opener


HOSTS = {"okx": "https://www.okx.com", "bitget": "https://api.bitget.com"}
SYMBOLS = ("BTC", "ETH", "SOL")
KST = timezone(timedelta(hours=9))

OKX_PATHS = {
    "instruments": "/api/v5/public/instruments",
    "oi_current": "/api/v5/public/open-interest",
    "oi_history": "/api/v5/rubik/stat/contracts/open-interest-history",
    "funding_current": "/api/v5/public/funding-rate",
    "funding_history": "/api/v5/public/funding-rate-history",
    "general_long_short": "/api/v5/rubik/stat/contracts/long-short-account-ratio-contract",
    "top_accounts": "/api/v5/rubik/stat/contracts/long-short-account-ratio-contract-top-trader",
    "top_positions": "/api/v5/rubik/stat/contracts/long-short-position-ratio-contract-top-trader",
    "liquidations": "/api/v5/public/liquidation-orders",
}
BITGET_PATHS = {
    "instruments": "/api/v3/market/instruments",
    "oi_current": "/api/v3/market/open-interest",
    "funding_current": "/api/v3/market/current-fund-rate",
    "funding_history": "/api/v3/market/history-fund-rate",
    "general_long_short": "/api/v3/market/futures-long-short",
    "active_accounts": "/api/v3/market/futures-account-long-short",
    "active_positions": "/api/v3/market/futures-position-long-short",
    "liquidations": "/api/v3/market/liquidations",
}


def _clean(value: object) -> str:
    return re.sub(r"[\x00-\x1f\x7f]", " ", str(value or ""))[:160]


def request(exchange: str, path: str, params: dict | None = None, *,
            opener=None, sleeper=time.sleep, timeout: int = 8) -> tuple[dict, object | None]:
    """Return bounded HTTP evidence and parsed JSON; retry only transient failures."""
    url = HOSTS[exchange] + path + ("?" + urlencode(params) if params else "")
    client = opener or build_opener()
    evidence = {"path": path, "http_status": None, "api_success": False,
                "api_code": None, "status": "FETCH_FAILED", "reason": None}
    for attempt in range(2):
        try:
            with client.open(Request(url, headers={"Accept": "application/json"}),
                             timeout=timeout) as response:
                evidence["http_status"] = getattr(response, "status", 200)
                body = response.read(2_000_000)
            payload = json.loads(body)
            code = payload.get("code") if isinstance(payload, dict) else None
            evidence["api_code"] = code
            evidence["api_success"] = str(code) == ("0" if exchange == "okx" else "00000")
            if evidence["api_success"]:
                evidence["status"] = "AVAILABLE"
                return evidence, payload
            evidence["reason"] = _clean(payload.get("msg") if isinstance(payload, dict) else "Invalid JSON shape")
            return evidence, payload
        except HTTPError as exc:
            evidence["http_status"] = exc.code
            raw = exc.read(512).decode("utf-8", errors="replace")
            try:
                parsed = json.loads(raw)
                evidence["reason"] = _clean(parsed.get("msg") or parsed.get("message")
                                            or parsed.get("error") or raw)
                evidence["api_code"] = parsed.get("code")
            except (ValueError, AttributeError):
                evidence["reason"] = _clean(re.sub(r"<[^>]+>", " ", raw))
            if exc.code in (429, 500, 502, 503, 504) and attempt == 0:
                sleeper(1)
                continue
            return evidence, None
        except (URLError, TimeoutError, ValueError) as exc:
            evidence["reason"] = _clean(exc)
            if attempt == 0:
                sleeper(1)
                continue
            return evidence, None
    return evidence, None


def _data(payload: object) -> object:
    return payload.get("data") if isinstance(payload, dict) else None


def _sample(value: object) -> object:
    """Retain only bounded observed fields; never synthesize market data."""
    if isinstance(value, dict):
        return {key: _sample(item) for key, item in list(value.items())[:16]}
    if isinstance(value, list):
        return [_sample(item) for item in value[:2]]
    return value if isinstance(value, (str, int, float, bool, type(None))) else str(value)[:100]


def _record(exchange: str, path: str, params: dict | None = None, *,
            getter=request, sleeper=time.sleep) -> dict:
    evidence, payload = getter(exchange, path, params)
    data = _data(payload)
    if evidence["api_success"]:
        if data in (None, [], {}):
            evidence["status"] = "NO_DATA"
        else:
            evidence["sample"] = _sample(data)
    return evidence


def _catalog(exchange: str, *, getter=request) -> tuple[dict, list[dict]]:
    path = (OKX_PATHS if exchange == "okx" else BITGET_PATHS)["instruments"]
    params = {"instType": "SWAP"} if exchange == "okx" else {"category": "USDT-FUTURES"}
    evidence, payload = getter(exchange, path, params)
    rows = _data(payload)
    if not isinstance(rows, list):
        evidence["status"] = "FETCH_FAILED" if not evidence["api_success"] else "INVALID_RESPONSE"
        evidence["reason"] = evidence["reason"] or "Instrument list is absent"
        return evidence, []
    evidence["instrument_count"] = len(rows)
    evidence["sample"] = [_sample(row) for row in rows if row.get("instId", row.get("symbol")) in
                          {f"{s}-USDT-SWAP" if exchange == "okx" else f"{s}USDT" for s in SYMBOLS}]
    return evidence, rows


def _matched(exchange: str, code: str, rows: list[dict]) -> dict | None:
    target = f"{code}-USDT-SWAP" if exchange == "okx" else f"{code}USDT"
    candidates = [row for row in rows if row.get("instId", row.get("symbol")) == target]
    if len(candidates) != 1:
        return None
    row = candidates[0]
    if exchange == "okx":
        valid = (row.get("instType") == "SWAP" and row.get("state") == "live"
                 and row.get("ctType") == "linear" and row.get("settleCcy") == "USDT"
                 and row.get("ctValCcy") == code)
    else:
        valid = (row.get("category") == "USDT-FUTURES" and row.get("baseCoin") == code
                 and row.get("quoteCoin") == "USDT" and row.get("type") == "perpetual"
                 and row.get("status") == "online" and row.get("symbolType") == "crypto")
    return row if valid else None


def probe(*, getter=request, sleeper=time.sleep) -> dict:
    now = datetime.now(UTC)
    result = {"schema_version": "1.0", "tested_at": {
        "utc": now.isoformat().replace("+00:00", "Z"), "kst": now.astimezone(KST).isoformat()},
        "environment": "github_actions" if __import__("os").getenv("GITHUB_ACTIONS") else "local",
        "exchanges": {}}
    for exchange in ("okx", "bitget"):
        catalog, rows = _catalog(exchange, getter=getter)
        provider = {"host": HOSTS[exchange], "instruments": catalog, "markets": {}}
        result["exchanges"][exchange] = provider
        paths = OKX_PATHS if exchange == "okx" else BITGET_PATHS
        for code in SYMBOLS:
            row = _matched(exchange, code, rows) if rows else None
            record = {"symbol": f"{code}-USDT-SWAP" if exchange == "okx" else f"{code}USDT",
                      "match_status": "VERIFIED" if row else
                      "FETCH_FAILED" if catalog["status"] != "AVAILABLE" else "UNVERIFIED",
                      "checks": {}}
            provider["markets"][code] = record
            if row is None:
                record["reason"] = (catalog["reason"] if catalog["status"] != "AVAILABLE"
                                    else "No uniquely verified live USDT perpetual contract")
                continue
            inst = record["symbol"]
            if exchange == "okx":
                checks = (
                    ("oi_current", {"instType": "SWAP", "instId": inst}),
                    ("oi_history", {"instId": inst, "period": "1H", "limit": 30}),
                    ("funding_current", {"instId": inst}),
                    ("funding_history", {"instId": inst, "limit": 2}),
                    ("general_long_short", {"instId": inst, "period": "1H", "limit": 1}),
                    ("top_accounts", {"instId": inst, "period": "1H", "limit": 1}),
                    ("top_positions", {"instId": inst, "period": "1H", "limit": 1}),
                    ("liquidations", {"instType": "SWAP", "uly": f"{code}-USDT",
                                      "state": "filled", "limit": 10}),
                )
            else:
                checks = (
                    ("oi_current", {"category": "USDT-FUTURES", "symbol": inst}),
                    ("funding_current", {"category": "USDT-FUTURES", "symbol": inst}),
                    ("funding_history", {"category": "USDT-FUTURES", "symbol": inst, "limit": 2}),
                    ("general_long_short", {"symbol": inst, "period": "1h"}),
                    ("active_accounts", {"symbol": inst, "period": "1h"}),
                    ("active_positions", {"symbol": inst, "period": "1h"}),
                    ("liquidations", {"category": "USDT-FUTURES", "symbol": inst, "limit": 10}),
                )
                record["checks"]["oi_history"] = {
                    "status": "NOT_SUPPORTED", "http_status": None, "api_success": None,
                    "reason": "No documented public historical OI endpoint"}
                for name in ("top_accounts", "top_positions"):
                    record["checks"][name] = {"status": "NOT_SUPPORTED", "http_status": None,
                                               "api_success": None,
                                               "reason": "Active-account/position metrics are not documented as Top Trader"}
            for name, params in checks:
                record["checks"][name] = _record(exchange, paths[name], params, getter=getter)
                if exchange == "bitget" and name in (
                    "general_long_short", "active_accounts", "active_positions"
                ):
                    sleeper(1.05)  # Official limit: 1 request/second/IP.
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("data/derivatives_provider_test.json"))
    args = parser.parse_args()
    result = probe()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, separators=(",", ":")),
                           encoding="utf-8")
    for exchange, provider in result["exchanges"].items():
        print(json.dumps({"exchange": exchange, "catalog": provider["instruments"]["status"],
                          "markets": {code: {"match": row["match_status"],
                             "checks": {name: info["status"] for name, info in row["checks"].items()}}
                                      for code, row in provider["markets"].items()}},
                         separators=(",", ":")), flush=True)
    return 0  # Network failures are evidence, not a reason to discard the report.


if __name__ == "__main__":
    raise SystemExit(main())
