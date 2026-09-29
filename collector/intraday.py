"""Bounded, observation-only Upbit hourly context.

Missing Upbit candles are not zero-volume observations: the official API
omits periods without trades.
"""

from __future__ import annotations

import argparse
import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import fmean

from collector.collection_targets import rotating_markets
from collector.main import write_json_atomic
from collector.upbit import RetryConfig, UpbitAPIError, UpbitClient

MAX_MARKETS = 20
HOURLY_CANDLES = 120
MAX_AGE = timedelta(hours=2)


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return (parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed).astimezone(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def select_targets(latest: dict, now: datetime,
                   limit: int = MAX_MARKETS) -> tuple[list[str], int]:
    """Rotate through valid Upbit observations without private state."""
    if limit < 1 or limit > MAX_MARKETS:
        raise ValueError("Hourly market limit is outside the API budget")
    return rotating_markets(latest, limit, int(now.timestamp() // (4 * 3600)))


def _number(value: object, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("Invalid hourly candle number")
    result = float(value)
    if (positive and result <= 0) or (not positive and result < 0):
        raise ValueError("Invalid hourly candle quantity")
    return result


def calculate_hourly(candles: list[dict], now: datetime, market: str) -> dict:
    """Use completed contiguous 1h windows; never compare partial or absent hours."""
    now = now.astimezone(UTC)
    complete: dict[datetime, dict] = {}
    for candle in candles:
        if candle.get("market") != market:
            raise ValueError("Hourly candle market mismatch")
        start = _utc(candle["candle_date_time_utc"])
        if start.minute or start.second or start + timedelta(hours=1) > now:
            continue
        complete[start] = candle
    ordered = sorted(complete.items())
    result = {"source": "UPBIT_COMPLETED_1H_CANDLES", "status": "INSUFFICIENT_HISTORY",
              "observed_at": _iso(ordered[-1][0] + timedelta(hours=1)) if ordered else None,
              "completed_hour_count": len(ordered), "price_change_pct_1h": None,
              "price_change_pct_4h": None, "rvol_1h": None, "rvol_4h": None,
              "relative_traded_value_1h": None, "relative_traded_value_4h": None,
              "traded_value_1h_krw": None, "traded_value_4h_krw": None,
              "missing_reason": None}
    if not ordered:
        result["missing_reason"] = "NO_COMPLETED_CANDLES"
        return result
    if now - (ordered[-1][0] + timedelta(hours=1)) > MAX_AGE:
        result.update(status="STALE", missing_reason="LAST_COMPLETED_HOUR_IS_STALE")
        return result
    def contiguous(count: int) -> bool:
        window = ordered[-count:]
        return len(window) == count and all(
            window[i][0] - window[i - 1][0] == timedelta(hours=1)
            for i in range(1, len(window)))
    try:
        if contiguous(21):
            current = ordered[-1][1]
            close = _number(current["trade_price"], positive=True)
            prior_close = _number(ordered[-2][1]["trade_price"], positive=True)
            volume = _number(current["candle_acc_trade_volume"])
            baseline = fmean(_number(row["candle_acc_trade_volume"])
                             for _, row in ordered[-21:-1])
            turnover = _number(current["candle_acc_trade_price"])
            turnover_base = fmean(_number(row["candle_acc_trade_price"])
                                  for _, row in ordered[-21:-1])
            result.update(price_change_pct_1h=round((close / prior_close - 1) * 100, 6),
                          rvol_1h=round(volume / baseline, 6) if baseline > 0 else None,
                          relative_traded_value_1h=round(turnover / turnover_base, 6)
                          if turnover_base > 0 else None,
                          traded_value_1h_krw=turnover)
        if contiguous(84):
            recent = [row for _, row in ordered[-4:]]
            baseline_rows = [row for _, row in ordered[-84:-4]]
            volume = fmean(_number(row["candle_acc_trade_volume"]) for row in recent)
            baseline = fmean(_number(row["candle_acc_trade_volume"])
                             for row in baseline_rows)
            turnover = fmean(_number(row["candle_acc_trade_price"]) for row in recent)
            turnover_base = fmean(_number(row["candle_acc_trade_price"])
                                  for row in baseline_rows)
            close = _number(recent[-1]["trade_price"], positive=True)
            prior_close = _number(ordered[-5][1]["trade_price"], positive=True)
            result.update(price_change_pct_4h=round((close / prior_close - 1) * 100, 6),
                          rvol_4h=round(volume / baseline, 6) if baseline > 0 else None,
                          relative_traded_value_4h=round(turnover / turnover_base, 6)
                          if turnover_base > 0 else None,
                          traded_value_4h_krw=round(sum(
                              _number(row["candle_acc_trade_price"]) for row in recent), 6))
    except (KeyError, TypeError, ValueError) as error:
        result.update(status="INVALID_SOURCE", missing_reason=str(error),
                      price_change_pct_1h=None, price_change_pct_4h=None,
                      rvol_1h=None, rvol_4h=None,
                      relative_traded_value_1h=None, relative_traded_value_4h=None,
                      traded_value_1h_krw=None, traded_value_4h_krw=None)
        return result
    result["status"] = "AVAILABLE" if result["rvol_1h"] is not None and result["rvol_4h"] is not None \
        else "PARTIAL" if result["price_change_pct_1h"] is not None else "INSUFFICIENT_HISTORY"
    if result["status"] != "AVAILABLE":
        result["missing_reason"] = "NONCONTIGUOUS_OR_INSUFFICIENT_COMPLETED_HOURS"
    return result


def collect(latest: dict, client: UpbitClient,
            *, now: datetime | None = None) -> dict:
    now = (now or datetime.now(UTC)).astimezone(UTC)
    markets, omitted = select_targets(latest, now)
    results = {}
    for code in markets:
        try:
            results[code] = calculate_hourly(client.get_minute_candles(code, 60, HOURLY_CANDLES),
                                             now, code)
        except (UpbitAPIError, KeyError, TypeError, ValueError) as error:
            results[code] = {"status": "FETCH_FAILED", "observed_at": None,
                             "missing_reason": str(error)}
    return {"schema_version": "1.0", "collected_at": _iso(now),
            "source_latest_collected_at": (latest.get("collected_at") or {}).get("utc"),
            "source": "Upbit public 60-minute candle API", "request_count": len(markets),
            "market_limit": MAX_MARKETS, "eligible_market_count": len(markets) + omitted,
            "omitted_market_count": omitted, "markets": results,
            "metric_definition": {
                "1h": "last completed hour versus preceding 20 complete non-overlapping hours",
                "4h": "last four complete hours versus preceding 80 complete non-overlapping hours",
                "missing_hour": "unknown, never filled with zero; Upbit omits hours without trades"}}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--latest", type=Path, default=Path("data/latest.json"))
    parser.add_argument("--output", type=Path, default=Path("data/intraday_summary.json"))
    args = parser.parse_args()
    latest = json.loads(args.latest.read_text(encoding="utf-8"))
    result = collect(latest, UpbitClient(
        retry=RetryConfig(attempts=2, backoff_seconds=0.5, timeout_seconds=5)))
    write_json_atomic(result, args.output, compact=True)
    print(f"Hourly Upbit context: {result['request_count']} requests, "
          f"{sum(row['status'] == 'AVAILABLE' for row in result['markets'].values())} complete, "
          f"{result['omitted_market_count']} outside bounded scope")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
