"""Historical candle collection and deterministic technical indicators."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta, timezone
from statistics import fmean, pstdev
from typing import Any, Sequence

from collector.upbit import UpbitAPIError, UpbitClient


KST = timezone(timedelta(hours=9), name="KST")
VALIDATION_SYMBOLS = ("BTC", "ETH", "XRP", "SOL")
INDICATOR_DEFINITIONS = {
    "price_change_pct": "(latest completed close / close N days ago - 1) * 100",
    "rvol": "mean volume of latest N days / mean volume of preceding 20 days",
    "volume_dry_up": "1d RVOL <= 0.5",
    "atr_14": "mean(max(high-low, abs(high-prev_close), abs(low-prev_close))) over 14 days",
    "bollinger_band_width_20_pct": "4 * population_stddev(close, 20) / SMA(close, 20) * 100",
    "recent_box": "longest trailing 5-20 day high-low range <= 10% of its low",
    "distance_from_20d_low_pct": "(latest completed close / lowest low up to 20 days - 1) * 100",
}


def _round(value: float) -> float:
    return round(value, 10)


def _value(candle: dict[str, Any], key: str) -> float:
    value = candle.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"candle field {key!r} is missing or non-numeric")
    value = float(value)
    if not math.isfinite(value):
        raise ValueError(f"candle field {key!r} is not finite")
    return value


def mark_daily_candles(
    candles: Sequence[dict[str, Any]], collected_at: datetime
) -> list[dict[str, Any]]:
    """Sort oldest-first and mark a UTC daily candle complete after its next boundary."""
    marked: list[dict[str, Any]] = []
    instant = collected_at.astimezone(UTC)
    for source in reversed(candles):
        candle = dict(source)
        start = datetime.fromisoformat(str(candle["candle_date_time_utc"]) + "+00:00")
        candle["is_complete"] = instant >= start + timedelta(days=1)
        marked.append(candle)
    return marked


def calculate_indicators(candles: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Calculate indicators from oldest-first, completed daily candles only."""
    completed = [candle for candle in candles if candle.get("is_complete") is True]
    closes = [_value(candle, "trade_price") for candle in completed]
    highs = [_value(candle, "high_price") for candle in completed]
    lows = [_value(candle, "low_price") for candle in completed]
    volumes = [_value(candle, "candle_acc_trade_volume") for candle in completed]

    def price_change(period: int) -> float | None:
        if len(closes) < period + 1 or closes[-period - 1] == 0:
            return None
        return _round((closes[-1] / closes[-period - 1] - 1) * 100)

    def rvol(period: int, baseline: int = 20) -> float | None:
        if len(volumes) < period + baseline:
            return None
        recent = fmean(volumes[-period:])
        reference = fmean(volumes[-period - baseline : -period])
        return None if reference == 0 else _round(recent / reference)

    atr14 = None
    if len(completed) >= 15:
        true_ranges = []
        for index in range(len(completed) - 14, len(completed)):
            previous_close = closes[index - 1]
            true_ranges.append(
                max(
                    highs[index] - lows[index],
                    abs(highs[index] - previous_close),
                    abs(lows[index] - previous_close),
                )
            )
        atr14 = _round(fmean(true_ranges))

    bollinger_width20 = None
    if len(closes) >= 20:
        window = closes[-20:]
        middle = fmean(window)
        if middle != 0:
            bollinger_width20 = _round((4 * pstdev(window) / middle) * 100)

    box_days = None if len(completed) < 5 else 0
    box_width = None
    # The recent box is the longest trailing 5-20 day range no wider than 10%.
    for period in range(5, min(20, len(completed)) + 1):
        low = min(lows[-period:])
        width = None if low == 0 else (max(highs[-period:]) - low) / low * 100
        if width is not None and width <= 10:
            box_days, box_width = period, _round(width)

    distance_from_low20 = None
    if closes and lows:
        recent_low = min(lows[-20:])
        if recent_low != 0:
            distance_from_low20 = _round((closes[-1] / recent_low - 1) * 100)

    dry_up_ratio = rvol(1)
    return {
        "as_of": completed[-1].get("candle_date_time_utc") if completed else None,
        "completed_candle_count": len(completed),
        "price_change_pct": {
            "1d": price_change(1),
            "3d": price_change(3),
            "7d": price_change(7),
        },
        "rvol": {"1d": dry_up_ratio, "3d": rvol(3), "7d": rvol(7)},
        "volume_dry_up": {
            "ratio_to_prior_20d": dry_up_ratio,
            "is_dry_up": None if dry_up_ratio is None else dry_up_ratio <= 0.5,
        },
        "atr_14": atr14,
        "bollinger_band_width_20_pct": bollinger_width20,
        "recent_box": {"duration_days": box_days, "width_pct": box_width},
        "distance_from_20d_low_pct": distance_from_low20,
    }


def _has_insufficient_data(indicators: dict[str, Any]) -> bool:
    required = [
        *indicators["price_change_pct"].values(),
        *indicators["rvol"].values(),
        indicators["atr_14"],
        indicators["bollinger_band_width_20_pct"],
        indicators["recent_box"]["duration_days"],
        indicators["distance_from_20d_low_pct"],
    ]
    return any(value is None for value in required)


def collect_minute_history(
    client: UpbitClient, market: str, unit: int, target_count: int
) -> list[dict[str, Any]]:
    """Page backward through the API and return minute candles oldest first."""
    newest_first: list[dict[str, Any]] = []
    to: str | None = None
    while len(newest_first) < target_count:
        requested = min(200, target_count - len(newest_first))
        batch = client.get_minute_candles(market, unit, requested, to=to)
        newest_first.extend(batch)
        if len(batch) < requested:
            break
        oldest_time = batch[-1].get("candle_date_time_utc")
        if not oldest_time:
            raise ValueError("minute candle is missing candle_date_time_utc")
        next_to = f"{oldest_time}Z"
        if next_to == to:
            break
        to = next_to
    return list(reversed(newest_first[:target_count]))


def build_indicators_snapshot(
    client: UpbitClient, minute_days: int = 0, minute_unit: int = 60
) -> dict[str, Any]:
    """Collect 60 daily candles per KRW market and calculate indicators."""
    if not 0 <= minute_days <= 3:
        raise ValueError("minute_days must be between 0 and 3")
    collected_at = datetime.now(UTC)
    market_details = client.get_krw_markets()
    records: list[dict[str, Any]] = []
    validation: dict[str, dict[str, Any]] = {
        symbol: {
            "market": f"KRW-{symbol}",
            "success": False,
            "daily_candle_count": 0,
            "reason": "market_not_listed",
        }
        for symbol in VALIDATION_SYMBOLS
    }

    for metadata in market_details:
        market = str(metadata["market"])
        errors: list[dict[str, str]] = []
        daily: list[dict[str, Any]] = []
        minute: list[dict[str, Any]] | None = None
        indicators = None
        try:
            daily = mark_daily_candles(client.get_daily_candles(market, 60), collected_at)
            indicators = calculate_indicators(daily)
        except (UpbitAPIError, KeyError, TypeError, ValueError) as exc:
            errors.append({"stage": "daily_candles", "reason": str(exc)})

        if minute_days:
            count = math.ceil(minute_days * 24 * 60 / minute_unit)
            try:
                minute = collect_minute_history(client, market, minute_unit, count)
            except (UpbitAPIError, KeyError, TypeError, ValueError) as exc:
                errors.append({"stage": "minute_candles", "reason": str(exc)})

        record = {
            "market": market,
            "metadata": metadata,
            "collection": {
                "success": not errors,
                "daily_success": bool(daily),
                "minute_success": None if not minute_days else minute is not None,
                "daily_candle_count": len(daily),
                "minute_candle_count": len(minute or []),
                "has_incomplete_daily_candle": any(
                    candle["is_complete"] is False for candle in daily
                ),
                "errors": errors,
            },
            "daily_candles": daily,
            "minute_candles": minute,
            "indicators": indicators,
        }
        records.append(record)
        symbol = market.removeprefix("KRW-")
        if symbol in VALIDATION_SYMBOLS:
            validation[symbol] = {
                "market": market,
                "success": not errors and indicators is not None,
                "daily_candle_count": len(daily),
                "reason": errors or None,
            }

    failed = sum(bool(record["collection"]["errors"]) for record in records)
    insufficient = sum(
        record["indicators"] is not None
        and _has_insufficient_data(record["indicators"])
        for record in records
    )
    return {
        "schema_version": "2.0",
        "source": "Upbit public API",
        "collected_at": {
            "utc": collected_at.isoformat().replace("+00:00", "Z"),
            "kst": collected_at.astimezone(KST).isoformat(),
        },
        "configuration": {
            "daily_candle_target": 60,
            "minute_days": minute_days,
            "minute_unit": minute_unit if minute_days else None,
            "uses_completed_daily_candles_for_indicators": True,
        },
        "indicator_definitions": INDICATOR_DEFINITIONS,
        "summary": {
            "market_count": len(records),
            "successful_market_count": len(records) - failed,
            "failed_market_count": failed,
            "insufficient_data_market_count": insufficient,
        },
        "validation_symbols": validation,
        "markets": records,
    }
