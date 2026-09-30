"""Reproducible per-market audit of unresolved futures asset identities."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from collector.asset_identity import diagnose_unverified
from collector.main import write_json_atomic


PROVIDERS = ("bitget", "gate", "kucoin")


def audit(snapshot: dict, latest: dict, cache: dict | None,
          baseline: dict | None = None) -> dict:
    names = {row["market"]: (row.get("metadata") or {}).get("english_name")
             for row in latest["markets"]}
    catalogs = {
        "bitget": {row.get("coin"): row for row in (cache or {}).get("bitget_coins") or []},
        "gate": {row.get("currency"): row for row in (cache or {}).get("gate_currencies") or []},
        "kucoin": {row.get("currency"): row for row in (cache or {}).get("kucoin_currencies") or []},
    }
    before = (baseline or {}).get("unverified") or {}
    out = {"schema_version": "1.0",
           "audited_at_utc": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
           "source_collected_at_utc": (snapshot.get("collected_at") or {}).get("utc"),
           "cache_checked_at_utc": (cache or {}).get("checked_at_utc"),
           "source_baseline_commit": (baseline or {}).get("source_commit"),
           "providers": {}}
    for provider in PROVIDERS:
        unverified = []
        newly_verified = []
        newly_with_oi = []
        newly_with_funding = []
        for code, row in snapshot["markets"].items():
            record = row.get(provider) or {}
            match = record.get("match") or {}
            status = match.get("status")
            if status == "UNVERIFIED":
                asset = code.removeprefix("KRW-")
                reason_code, evidence = diagnose_unverified(
                    code, names.get(code), provider, catalogs[provider].get(asset), cache)
                reason = match.get("reason") or ""
                if "contract" in reason.lower() or "multiplier" in reason.lower():
                    reason_code = "CONTRACT_UNIT_UNCLEAR"
                if "multiple candidate" in reason.lower():
                    reason_code = "SYMBOL_COLLISION"
                unverified.append({"market": code, "symbol": match.get("symbol"),
                                   "upbit_name": names.get(code),
                                   "reason_code": reason_code, "collector_reason": reason,
                                   "evidence": evidence})
            if status == "VERIFIED" and code in before.get(provider, []):
                newly_verified.append(code)
                if (record.get("oi") or {}).get("exchange_quantity") is not None:
                    newly_with_oi.append(code)
                if (record.get("funding") or {}).get("current_rate") is not None:
                    newly_with_funding.append(code)
        counts = (snapshot.get("summary") or {}).get("provider_counts", {}).get(provider, {})
        out["providers"][provider] = {
            "before": (baseline or {}).get("provider_counts", {}).get(provider),
            "after": {"verified": counts.get("verified"), "unverified": counts.get("unverified"),
                      "oi_available": counts.get("oi_available"),
                      "funding_available": counts.get("funding_available"),
                      "request_count": counts.get("request_count"),
                      "duration_seconds": counts.get("duration_seconds")},
            "newly_verified": newly_verified,
            "newly_with_oi": newly_with_oi,
            "newly_with_funding": newly_with_funding,
            "still_unverified": unverified}
    out["identity_api_requests"] = (cache or {}).get("request_counts")
    out["identity_api_errors"] = (cache or {}).get("catalog_errors")
    out["identity_ticker_diagnostics"] = (cache or {}).get("ticker_meta")
    return out


def write_audit(snapshot: dict, latest: dict, cache: dict | None,
                baseline_path: Path, output: Path) -> dict:
    try:
        with baseline_path.open(encoding="utf-8") as handle:
            baseline = json.load(handle)
    except (OSError, ValueError):
        baseline = None
    result = audit(snapshot, latest, cache, baseline)
    write_json_atomic(result, output, compact=True)
    return result
