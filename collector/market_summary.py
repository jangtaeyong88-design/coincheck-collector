"""Build a small, complete per-market view from the two collected snapshots."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from collector.main import KST


FIELDS = (
    "price",
    "price_change_pct_1d",
    "price_change_pct_3d",
    "price_change_pct_7d",
    "rvol_1d",
    "rvol_3d",
    "rvol_7d",
    "volume_dry_up",
    "atr_14",
    "bollinger_band_width_20_pct",
    "box_duration_days",
    "box_width_pct",
    "distance_from_20d_low_pct",
    "collection_success",
    "insufficient_data",
    "latest_available",
    "indicators_available",
    "box_status",
    "daily_candle_ended_at",
)


def _markets_by_code(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for record in snapshot["markets"]:
        code = record["market"]
        if not isinstance(code, str) or not code.startswith("KRW-") or code in records:
            raise ValueError(f"invalid or duplicate KRW market: {code!r}")
        records[code] = record
    return records


def _source_time(snapshot: dict[str, Any]) -> dict[str, str | None]:
    collected_at = snapshot.get("collected_at") or {}
    return {"utc": collected_at.get("utc"), "kst": collected_at.get("kst")}


def _time_difference_seconds(
    latest_time: dict[str, str | None], indicator_time: dict[str, str | None]
) -> float | None:
    if not latest_time["utc"] or not indicator_time["utc"]:
        return None
    latest = datetime.fromisoformat(latest_time["utc"].replace("Z", "+00:00"))
    indicators = datetime.fromisoformat(indicator_time["utc"].replace("Z", "+00:00"))
    if latest.utcoffset() is None or indicators.utcoffset() is None:
        raise ValueError("source UTC timestamps must include a time zone")
    return round((indicators - latest).total_seconds(), 3)


def _completed_daily_end(as_of: str | None) -> str | None:
    if not isinstance(as_of, str) or not as_of:
        return None
    try:
        started = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
    except ValueError:
        return None
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    return (started.astimezone(UTC) + timedelta(days=1)).isoformat().replace("+00:00", "Z")


def build_market_summary(
    latest: dict[str, Any], indicators: dict[str, Any], *, generated_at: datetime | None = None
) -> dict[str, Any]:
    """Preserve all KRW markets from either source; never estimate missing values."""
    latest_markets = _markets_by_code(latest)
    indicator_markets = _markets_by_code(indicators)
    market_codes = sorted(latest_markets.keys() | indicator_markets.keys())
    if not market_codes:
        raise ValueError("source snapshots contain no KRW markets")

    rows: dict[str, list[Any]] = {}
    successful = 0
    insufficient = 0
    for code in market_codes:
        latest_record = latest_markets.get(code)
        indicator_record = indicator_markets.get(code)
        ticker = (latest_record or {}).get("ticker") or {}
        values = (indicator_record or {}).get("indicators") or {}
        changes = values.get("price_change_pct") or {}
        rvol = values.get("rvol") or {}
        dry_up = values.get("volume_dry_up") or {}
        box = values.get("recent_box") or {}
        box_days = box.get("duration_days")
        box_status = ("INSUFFICIENT_HISTORY" if box_days is None else
                      "NO_BOX" if box_days == 0 else "FOUND")
        collection = (indicator_record or {}).get("collection") or {}

        is_insufficient = bool(values) and any(
            value is None
            for value in (
                changes.get("1d"), changes.get("3d"), changes.get("7d"),
                rvol.get("1d"), rvol.get("3d"), rvol.get("7d"),
                values.get("atr_14"),
                values.get("bollinger_band_width_20_pct"),
                box.get("duration_days"),
                values.get("distance_from_20d_low_pct"),
            )
        )
        success = bool(
            latest_record
            and indicator_record
            and ticker
            and values
            and not latest_record.get("errors")
            and collection.get("success") is True
        )
        successful += success
        insufficient += is_insufficient
        rows[code] = [
            ticker.get("trade_price"),
            changes.get("1d"), changes.get("3d"), changes.get("7d"),
            rvol.get("1d"), rvol.get("3d"), rvol.get("7d"),
            dry_up.get("is_dry_up"),
            values.get("atr_14"),
            values.get("bollinger_band_width_20_pct"),
            box.get("duration_days"), box.get("width_pct"),
            values.get("distance_from_20d_low_pct"),
            success,
            is_insufficient,
            latest_record is not None,
            indicator_record is not None,
            box_status,
            _completed_daily_end(values.get("as_of")),
        ]

    instant = (generated_at or datetime.now(UTC)).astimezone(UTC)
    latest_time = _source_time(latest)
    indicator_time = _source_time(indicators)
    return {
        "schema_version": "1.0",
        "generated_at": {
            "utc": instant.isoformat().replace("+00:00", "Z"),
            "kst": instant.astimezone(KST).isoformat(),
        },
        "source_collected_at": {
            "latest": latest_time,
            "indicators": indicator_time,
        },
        "indicators_minus_latest_seconds": _time_difference_seconds(
            latest_time, indicator_time
        ),
        "summary": {
            "market_count": len(rows),
            "successful_market_count": successful,
            "failed_market_count": len(rows) - successful,
            "insufficient_data_market_count": insufficient,
        },
        "fields": list(FIELDS),
        "markets": rows,
    }
