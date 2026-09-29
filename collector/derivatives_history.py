"""Bounded, timestamped observations from the Bitget public API."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from collector.derivatives import _number, _timestamp


RETENTION_DAYS = 35
DAY_MS = 86_400_000
HOUR_MS = 3_600_000
REFERENCE_TOLERANCE_MS = 10 * 60_000
SERIES = ("oi", "funding_current", "funding_settled", "general_accounts",
          "active_accounts", "active_positions")
OPTIONAL_SERIES = ("oi", "funding_current", "funding_settled", "general_accounts",
                   "top_accounts", "top_positions")


def add_observation_coverage(snapshot: dict) -> None:
    """Show actual available deltas separately from current OI and funding."""
    def available_delta(row: dict, horizon: str) -> bool:
        change = ((row.get("oi") or {}).get("changes") or {}).get(horizon)
        return isinstance(change, dict) and change.get("status") == "AVAILABLE" and \
            _number(change.get("pct")) is not None

    counts = snapshot.get("summary", {}).get("provider_counts", {})
    for provider in ("bitget", "gate", "kucoin"):
        if provider not in counts:
            continue
        rows = [pair.get(provider) or {} for pair in snapshot.get("markets", {}).values()]
        verified = [row for row in rows if (row.get("match") or {}).get("status") == "VERIFIED"]
        counts[provider]["oi_change_available"] = {
            horizon: sum(available_delta(row, horizon) for row in verified)
            for horizon in ("1h", "4h", "24h")}
        counts[provider]["funding_observed"] = sum(
            _number((row.get("funding") or {}).get("current_rate")) is not None or
            _number((row.get("funding") or {}).get("latest_rate")) is not None
            for row in verified)


def load_history(path: Path) -> dict:
    if not path.exists():
        return {"schema_version": "1.0", "retention_days": RETENTION_DAYS,
                "provider": "bitget", "markets": {}}
    with path.open(encoding="utf-8") as handle:
        history = json.load(handle)
    if not isinstance(history, dict) or history.get("schema_version") != "1.0" \
            or not isinstance(history.get("markets"), dict):
        raise ValueError("Unknown or invalid derivatives history; refusing to discard it")
    return history


def _merge_series(existing: Any, incoming: list[list], cutoff: int,
                  now_ms: int) -> list[list]:
    values: dict[int, float] = {}
    for row in (existing if isinstance(existing, list) else []) + incoming:
        if not isinstance(row, list) or len(row) != 2:
            continue
        at, value = _timestamp(row[0]), _number(row[1])
        if at is not None and cutoff <= at <= now_ms + 300_000 and value is not None:
            values.setdefault(at, value)
    return [[at, values[at]] for at in sorted(values)]


def _reference(series: list[list], current_at: int, hours: int) -> list | None:
    target = current_at - hours * HOUR_MS
    candidates = [row for row in series if row[0] < current_at
                  and abs(row[0] - target) <= REFERENCE_TOLERANCE_MS]
    return min(candidates, key=lambda row: abs(row[0] - target)) if candidates else None


def update_history(history: dict, snapshot: dict, *, now_ms: int | None = None) -> dict:
    """Merge real observations, de-duplicate timestamps and calculate bounded OI deltas."""
    now_ms = now_ms if now_ms is not None else int(datetime.now(UTC).timestamp() * 1000)
    cutoff = now_ms - RETENTION_DAYS * DAY_MS
    history["retention_days"] = RETENTION_DAYS
    history["provider"] = "bitget"
    history["updated_at_utc"] = datetime.fromtimestamp(now_ms / 1000, UTC).isoformat().replace("+00:00", "Z")
    for code, pair in snapshot["markets"].items():
        bitget = pair.get("bitget") or {}
        match = bitget.get("match") or {}
        if match.get("status") != "VERIFIED":
            continue
        symbol = match["symbol"]
        entry = history["markets"].get(code)
        if not isinstance(entry, dict) or entry.get("symbol") != symbol:
            entry = {"symbol": symbol, **{key: [] for key in SERIES}}
            history["markets"][code] = entry
        oi = bitget.get("oi") or {}
        funding = bitget.get("funding") or {}
        ratios = bitget.get("long_short") or {}
        incoming = {key: [] for key in SERIES}
        if _timestamp(oi.get("at_ms")) and _number(oi.get("exchange_quantity")) is not None:
            incoming["oi"].append([_timestamp(oi["at_ms"]), _number(oi["exchange_quantity"])])
        if _timestamp(funding.get("current_observed_at_ms")) and _number(funding.get("current_rate")) is not None:
            incoming["funding_current"].append([
                _timestamp(funding["current_observed_at_ms"]), _number(funding["current_rate"])])
        for prefix in ("latest", "previous"):
            if _timestamp(funding.get(f"{prefix}_at_ms")) and _number(funding.get(f"{prefix}_rate")) is not None:
                incoming["funding_settled"].append([
                    _timestamp(funding[f"{prefix}_at_ms"]), _number(funding[f"{prefix}_rate"])])
        for name in ("general_accounts", "active_accounts", "active_positions"):
            row = ratios.get(name) or {}
            if _timestamp(row.get("at_ms")) and _number(row.get("ratio")) is not None:
                incoming[name].append([_timestamp(row["at_ms"]), _number(row["ratio"])])
        for name in SERIES:
            entry[name] = _merge_series(entry.get(name), incoming[name], cutoff, now_ms)
        if not oi:
            continue
        oi["local_history_status"] = "AVAILABLE" if len(entry["oi"]) > 1 else "INSUFFICIENT_HISTORY"
        current_at, current = _timestamp(oi.get("at_ms")), _number(oi.get("exchange_quantity"))
        for hours in (4, 24):
            change = oi["changes"][f"{hours}h"]
            change.update({"pct": None, "reference_at_ms": None,
                           "elapsed_ms": None, "status": "INSUFFICIENT_HISTORY"})
            if current_at is None or current is None:
                continue
            reference = _reference(entry["oi"], current_at, hours)
            if reference is None or reference[1] <= 0:
                continue
            change.update({"pct": round((current / reference[1] - 1) * 100, 8),
                           "reference_at_ms": reference[0],
                           "elapsed_ms": current_at - reference[0],
                           "status": "AVAILABLE"})
        # Four-hour scheduled observations cannot establish a one-hour change.
        oi["changes"]["1h"].update({"pct": None, "reference_at_ms": None,
                                      "status": "INSUFFICIENT_HISTORY"})
    for code in list(history["markets"]):
        entry = history["markets"][code]
        for name in SERIES:
            entry[name] = _merge_series(entry.get(name), [], cutoff, now_ms)
        if not any(entry[name] for name in SERIES):
            del history["markets"][code]
    history["summary"] = {"market_count": len(history["markets"]),
                          "oi_observation_count": sum(len(row["oi"]) for row in history["markets"].values()),
                          "retained_from_ms": cutoff}
    return history


def update_optional_history(history: dict, snapshot: dict, *, now_ms: int | None = None) -> dict:
    """Retain Gate/KuCoin observations separately; never mix exchange OI series."""
    now_ms = now_ms if now_ms is not None else int(datetime.now(UTC).timestamp() * 1000)
    cutoff = now_ms - RETENTION_DAYS * DAY_MS
    providers = history.setdefault("providers", {})
    for provider in ("gate", "kucoin"):
        markets = providers.setdefault(provider, {})
        for code, pair in snapshot["markets"].items():
            data = pair.get(provider) or {}
            match = data.get("match") or {}
            if match.get("status") != "VERIFIED":
                continue
            symbol = match["symbol"]
            entry = markets.get(code)
            if not isinstance(entry, dict) or entry.get("symbol") != symbol:
                entry = {"symbol": symbol, **{key: [] for key in OPTIONAL_SERIES}}
                markets[code] = entry
            oi = data.get("oi") or {}
            funding = data.get("funding") or {}
            ratios = data.get("long_short") or {}
            incoming = {key: [] for key in OPTIONAL_SERIES}
            if _timestamp(oi.get("at_ms")) and _number(oi.get("exchange_quantity")) is not None:
                incoming["oi"].append([_timestamp(oi["at_ms"]), _number(oi["exchange_quantity"])])
            if _timestamp(funding.get("current_observed_at_ms")) and _number(funding.get("current_rate")) is not None:
                incoming["funding_current"].append([
                    _timestamp(funding["current_observed_at_ms"]), _number(funding["current_rate"])])
            for prefix in ("latest", "previous"):
                if _timestamp(funding.get(f"{prefix}_at_ms")) and _number(funding.get(f"{prefix}_rate")) is not None:
                    incoming["funding_settled"].append([
                        _timestamp(funding[f"{prefix}_at_ms"]), _number(funding[f"{prefix}_rate"])])
            for name in ("general_accounts", "top_accounts", "top_positions"):
                row = ratios.get(name) or {}
                if _timestamp(row.get("at_ms")) and _number(row.get("ratio")) is not None:
                    incoming[name].append([_timestamp(row["at_ms"]), _number(row["ratio"])])
            for name in OPTIONAL_SERIES:
                entry[name] = _merge_series(entry.get(name), incoming[name], cutoff, now_ms)
        for code in list(markets):
            entry = markets[code]
            for name in OPTIONAL_SERIES:
                entry[name] = _merge_series(entry.get(name), [], cutoff, now_ms)
            if not any(entry[name] for name in OPTIONAL_SERIES):
                del markets[code]
    history["summary"]["optional_oi_observation_count"] = {
        provider: sum(len(entry["oi"]) for entry in markets.values())
        for provider, markets in providers.items()}
    return history
