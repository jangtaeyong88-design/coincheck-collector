"""Persist GitHub Actions outcome separately from actual data-collection quality."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from collector.time_utils import SCHEMA_VERSION as VERSION, iso, utc
from collector.main import write_json_atomic
from collector.collection_quality import quality_result


def _read(path: Path) -> dict | None:
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


PROVIDER_NAMES = ("bitget", "gate", "kucoin")
COLLECTION_TRIGGERS = ("SCHEDULE", "RECOVERY", "CLOUDFLARE_SCHEDULE",
                       "CLOUDFLARE_RETRY", "MANUAL", "ANDROID_MANUAL")
STAGES = {"UPBIT", "INDICATORS", "MARKET_SUMMARY", "DERIVATIVES",
          "BITGET", "GATE", "KUCOIN", "FINALIZING", "PUBLISH_FAILED"}


def start(path: Path, run_id: str | None = None, slot: str | None = None,
          run_mode: str = "SCHEDULED", requested_at: str | None = None,
          trigger: str | None = None) -> dict:
    if run_mode not in ("SCHEDULED", "MANUAL") or (run_mode == "MANUAL" and slot is not None):
        raise ValueError("Invalid collection run mode or slot")
    if requested_at is not None:
        utc(requested_at)
    trigger = trigger or ("MANUAL" if run_mode == "MANUAL" else "SCHEDULE")
    if trigger not in COLLECTION_TRIGGERS or \
            (trigger in ("MANUAL", "ANDROID_MANUAL")) != (run_mode == "MANUAL") or \
            (trigger == "RECOVERY" and slot is None):
        raise ValueError("Invalid collection trigger")
    previous = _read(path) or {}
    now = iso(datetime.now(UTC))
    providers = copy.deepcopy(previous.get("providers") or {})
    old_derivatives = _read(path.with_name("derivatives_summary.json")) or {}
    old_observed = (old_derivatives.get("collected_at") or {}).get("utc")
    for name in PROVIDER_NAMES:
        provider = providers.get(name) or {}
        if not provider.get("last_success_at") and provider.get("status") in ("SUCCESS", "PARTIAL") \
                and (provider.get("verified") or 0) > 0:
            provider["last_success_at"] = _provider_observed_at(old_derivatives, name) or old_observed
        providers[name] = provider
        provider["last_success_before_run"] = provider.get("last_success_at")
    status = {"schema_version": VERSION,
              "run": {"id": run_id, "collection_run_id": run_id,
                      "run_mode": run_mode, "trigger": trigger,
                      "requested_at": requested_at or now,
                      "collection_slot": slot, "started_at": now,
                      "ended_at": None, "actions_result": "RUNNING",
                      "collection_result": "RUNNING", "duration_seconds": None,
                      "current_stage": "UPBIT",
                      "last_success_before_run": previous.get("last_success_at"),
                      "data_collected_before_run": previous.get("data_collected_at"),
                      "last_scheduled_success_before_run": previous.get("last_scheduled_success"),
                      "last_manual_success_before_run": previous.get("last_manual_success"),
                      "last_successful_collection_before_run": previous.get("last_successful_collection")},
              "last_success_at": previous.get("last_success_at"),
              "last_failure_at": previous.get("last_failure_at"),
              "last_scheduled_run": previous.get("last_scheduled_run"),
              "last_scheduled_success": previous.get("last_scheduled_success"),
              "last_manual_run": previous.get("last_manual_run"),
              "last_manual_success": previous.get("last_manual_success"),
              "last_successful_collection": previous.get("last_successful_collection"),
              "data_collected_at": previous.get("data_collected_at"),
              "updated_at": now,
              "upbit": previous.get("upbit"),
              "providers": providers,
              "errors": []}
    write_json_atomic(status, path, compact=True)
    return status


def stage(path: Path, name: str) -> dict:
    if name not in STAGES:
        raise ValueError(f"Unknown collection stage: {name}")
    status = _read(path)
    if not status or status["run"]["collection_result"] != "RUNNING":
        raise ValueError("Cannot change stage outside a running collection")
    status["run"]["current_stage"] = name
    status["updated_at"] = iso(datetime.now(UTC))
    write_json_atomic(status, path, compact=True)
    return status


def provider_start(path: Path, name: str) -> dict:
    if name not in PROVIDER_NAMES:
        raise ValueError(f"Unknown provider: {name}")
    status = stage(path, name.upper())
    provider = status["providers"].setdefault(name, {})
    provider.update({"status": "RUNNING", "started_at": status["updated_at"],
                     "ended_at": None, "duration_seconds": None, "error": None})
    write_json_atomic(status, path, compact=True)
    return status


def _provider_result(counts: dict) -> str:
    if not counts or (counts.get("verified") or 0) <= 0:
        return "FAILED"
    if (counts.get("missing_core_count") or 0) >= counts["verified"]:
        return "FAILED"
    if counts.get("fetch_failed", 0) or counts.get("data_fetch_failed", 0) \
            or counts.get("missing_core_count", 0):
        return "PARTIAL"
    return "SUCCESS"


def _provider_observed_at(snapshot: dict, name: str) -> str | None:
    observed_ms = []
    for pair in (snapshot.get("markets") or {}).values():
        row = pair.get(name) or {}
        if (row.get("match") or {}).get("status") != "VERIFIED":
            continue
        oi = row.get("oi") or {}
        funding = row.get("funding") or {}
        for value in (oi.get("at_ms"), funding.get("current_observed_at_ms")):
            if isinstance(value, (int, float)) and value > 0:
                observed_ms.append(value)
    if not observed_ms:
        return None
    return iso(datetime.fromtimestamp(max(observed_ms) / 1000, UTC))


def _update_provider(status: dict, name: str, snapshot: dict,
                     *, published: bool) -> None:
    counts = (snapshot.get("summary") or {}).get("provider_counts", {}).get(name) or {}
    diagnostics = (snapshot.get("exchange_diagnostics") or {}).get(name)
    provider = status["providers"].setdefault(name, {})
    now = datetime.now(UTC)
    result = _provider_result(counts)
    already_finished = published and provider.get("ended_at") is not None \
        and provider.get("status") in ("SUCCESS", "PARTIAL", "FAILED")
    provider.update({"status": result, "ended_at": provider["ended_at"] if already_finished else iso(now),
                     "verified": counts.get("verified"),
                     "unverified": counts.get("unverified"),
                     "no_market": counts.get("no_market", counts.get("absent")),
                     "api_requests": counts.get("request_count"),
                     "missing_core_rate": counts.get("missing_core_rate"),
                     "error": diagnostics if isinstance(diagnostics, str) or diagnostics is None
                     else json.dumps(diagnostics, ensure_ascii=False, separators=(",", ":"))[:300]})
    started = provider.get("started_at")
    if not already_finished:
        provider["duration_seconds"] = round((now - utc(started)).total_seconds(), 2) \
            if started else counts.get("duration_seconds")
    if published and result in ("SUCCESS", "PARTIAL"):
        observed = _provider_observed_at(snapshot, name)
        if observed:
            provider["last_success_at"] = observed


def provider_finish(path: Path, name: str, snapshot: dict) -> dict:
    if name not in PROVIDER_NAMES:
        raise ValueError(f"Unknown provider: {name}")
    status = _read(path)
    if not status or status["run"]["collection_result"] != "RUNNING":
        raise ValueError("Cannot finish provider outside a running collection")
    _update_provider(status, name, snapshot, published=False)
    status["updated_at"] = iso(datetime.now(UTC))
    write_json_atomic(status, path, compact=True)
    return status


def finish(path: Path, data_dir: Path, actions_result: str,
           *, run_id: str | None = None) -> dict:
    status = _read(path)
    current_run_id = run_id or os.getenv("GITHUB_RUN_ID")
    if not status or status.get("schema_version") != VERSION:
        raise ValueError("Cannot finish a collection that has not started")
    if current_run_id and str((status.get("run") or {}).get("collection_run_id") or
                              (status.get("run") or {}).get("id")) != str(current_run_id):
        raise ValueError("Cannot finish a different GitHub Actions run")
    now = datetime.now(UTC)
    run = status["run"]
    started = utc(run["started_at"])
    run["ended_at"] = iso(now)
    run["actions_result"] = actions_result.upper()
    run["duration_seconds"] = round((now - started).total_seconds(), 2)
    errors = []
    latest = _read(data_dir / "latest.json")
    indicators = _read(data_dir / "indicators.json")
    market = _read(data_dir / "market_summary.json")
    derivatives = _read(data_dir / "derivatives_summary.json")
    upbit = (latest or {}).get("summary") or {}
    status["upbit"] = {"market_count": upbit.get("market_count"),
                       "success_count": upbit.get("successful_market_count"),
                       "failure_count": upbit.get("failed_market_count")}
    status["data_collected_at"] = (latest or {}).get("collected_at", {}).get("utc")
    core_ok = True
    for name, document in (("latest", latest), ("indicators", indicators),
                           ("market_summary", market)):
        time_field = "generated_at" if name == "market_summary" else "collected_at"
        observed = (document or {}).get(time_field, {}).get("utc")
        if not observed:
            errors.append(f"{name}: missing collection timestamp")
            core_ok = False
            continue
        try:
            if utc(observed) < started:
                errors.append(f"{name}: stale snapshot")
                core_ok = False
        except ValueError:
            errors.append(f"{name}: invalid collection timestamp")
            core_ok = False
    count = upbit.get("market_count")
    core_quality = quality_result(upbit, count)
    for name, document in (("indicators", indicators), ("market_summary", market)):
        quality = quality_result((document or {}).get("summary") or {}, count)
        if quality == "FAILED":
            core_quality = "FAILED"
            errors.append(f"{name}: collection quality below threshold or invalid counts")
        elif quality == "PARTIAL_ACCEPTABLE" and core_quality == "SUCCESS":
            core_quality = quality
    if core_quality == "FAILED":
        errors.append("upbit: collection quality below threshold or invalid counts")
        core_ok = False
    elif core_quality == "PARTIAL_ACCEPTABLE":
        errors.append("upbit: bounded market-level omissions")
    if all(isinstance((doc or {}).get("markets"), list) for doc in (latest, indicators)) and \
            isinstance((market or {}).get("markets"), dict):
        sets = [set(row.get("market") for row in doc["markets"] if isinstance(row, dict))
                for doc in (latest, indicators)] + [set(market["markets"])]
        if any(len(codes) != count for codes in sets) or len(set(map(frozenset, sets))) != 1:
            errors.append("core: market sets differ")
            core_ok = False
    else:
        errors.append("core: invalid market list")
        core_ok = False
    fields = (market or {}).get("fields")
    if not isinstance(fields, list) or any(not isinstance(field, str) for field in fields) or \
            len(fields) != len(set(fields)) or not \
            {"price", "collection_success", "insufficient_data"}.issubset(fields):
        errors.append("market_summary: invalid fields")
        core_ok = False
    else:
        rows = market["markets"]
        success_index = fields.index("collection_success")
        row_success = 0
        for row in rows.values():
            if not isinstance(row, list) or len(row) != len(fields) or \
                    type(row[success_index]) is not bool:
                core_ok = False
                break
            row_success += row[success_index]
        if not core_ok or row_success != (market.get("summary") or {}).get("successful_market_count"):
            errors.append("market_summary: row success count mismatch")
            core_ok = False
    derivative_time = (derivatives or {}).get("collected_at", {}).get("utc")
    derivatives_fresh = False
    if not derivative_time:
        errors.append("derivatives: missing collection timestamp")
    else:
        try:
            if utc(derivative_time) < started:
                errors.append("derivatives: stale snapshot")
            else:
                derivatives_fresh = True
        except ValueError:
            errors.append("derivatives: invalid collection timestamp")
    if not isinstance((derivatives or {}).get("markets"), dict) or \
            (derivatives.get("summary") or {}).get("market_count") != count or \
            set(derivatives["markets"]) != (sets[0] if core_ok else set()):
        errors.append("derivatives: market set differs")
        core_ok = False
    for name in PROVIDER_NAMES:
        if derivatives_fresh and actions_result.upper() == "SUCCESS":
            _update_provider(status, name, derivatives or {}, published=True)
        else:
            provider = status["providers"].setdefault(name, {})
            provider.update({"status": "FAILED", "ended_at": iso(now),
                             "error": "Collection ended without a published derivatives snapshot"})
        if status["providers"][name].get("status") != "SUCCESS":
            errors.append(f"{name}: {status['providers'][name].get('status')}")
    if not core_ok or not derivatives_fresh or actions_result.upper() != "SUCCESS":
        result = "FAILED"
    elif errors:
        result = "PARTIAL_ACCEPTABLE"
    else:
        result = "SUCCESS"
    run["collection_result"] = result
    run["current_stage"] = "FINALIZING"
    if result in ("SUCCESS", "PARTIAL_ACCEPTABLE"):
        status["last_success_at"] = iso(now)
    else:
        status["last_failure_at"] = iso(now)
    status["updated_at"] = iso(now)
    status["errors"] = errors[:30]
    _record_run(status, data_dir)
    write_json_atomic(status, path, compact=True)
    return status


def _record_run(status: dict, data_dir: Path | None = None) -> None:
    run = status["run"]
    mode = run.get("run_mode", "SCHEDULED")
    if mode not in ("SCHEDULED", "MANUAL"):
        return
    sources = {}
    if data_dir is not None:
        for name in ("latest", "indicators", "market_summary", "derivatives_summary"):
            document = _read(data_dir / f"{name}.json") or {}
            field = "generated_at" if name == "market_summary" else "collected_at"
            sources[name] = (document.get(field) or {}).get("utc")
    latest_path = data_dir / "latest.json" if data_dir is not None else None
    digest = "sha256:" + hashlib.sha256(latest_path.read_bytes()).hexdigest() \
        if latest_path is not None and latest_path.is_file() else None
    summary = {"collection_run_id": run.get("collection_run_id") or run.get("id"),
               "run_mode": mode, "trigger": run.get("trigger"),
               "collection_slot": run.get("collection_slot"),
               "requested_at": run.get("requested_at"),
               "started_at": run.get("started_at"), "ended_at": run.get("ended_at"),
               "actions_result": run.get("actions_result"),
               "collection_result": run.get("collection_result"),
               "data_collected_at": status.get("data_collected_at"),
               "source_collected_at": sources,
               "market_snapshot_id": digest,
               "upbit": copy.deepcopy(status.get("upbit"))}
    prefix = "last_scheduled" if mode == "SCHEDULED" else "last_manual"
    status[f"{prefix}_run"] = summary
    if run.get("actions_result") == "SUCCESS" and run.get("collection_result") in \
            ("SUCCESS", "PARTIAL_ACCEPTABLE"):
        status[f"{prefix}_success"] = copy.deepcopy(summary)
        status["last_successful_collection"] = copy.deepcopy(summary)


def publish_failed(path: Path, reason: str, *, run_id: str | None = None,
                   run_mode: str | None = None, slot: str | None = None,
                   trigger: str | None = None, requested_at: str | None = None) -> dict:
    status = _read(path)
    github_run_id = os.getenv("GITHUB_RUN_ID")
    if github_run_id and run_id and str(run_id) != github_run_id:
        raise ValueError("Failure run ID does not match GITHUB_RUN_ID")
    current_run_id = github_run_id or run_id
    existing_run = (status or {}).get("run") or {}
    existing_run_id = existing_run.get("collection_run_id") or existing_run.get("id")
    if current_run_id is None:
        # Local callers may finalize a status they explicitly started. In Actions,
        # GITHUB_RUN_ID always binds the failure to the current workflow run.
        current_run_id = existing_run_id
    if not current_run_id:
        raise ValueError("Cannot publish failure without a current run ID")
    if str(existing_run_id) != str(current_run_id):
        # A start/argparse failure leaves the previous published run on disk.
        # Never rewrite that run as this workflow's failure.
        if run_mode is None or trigger is None:
            raise ValueError("Run provenance is required before recording a pre-start failure")
        status = start(path, str(current_run_id), slot, run_mode, requested_at, trigger)
    now = datetime.now(UTC)
    run = status["run"]
    status["last_success_at"] = run.get("last_success_before_run")
    status["data_collected_at"] = run.get("data_collected_before_run")
    for key in ("last_scheduled_success", "last_manual_success",
                "last_successful_collection"):
        if (status.get(key) or {}).get("collection_run_id") == run.get("collection_run_id"):
            status[key] = run.get(f"{key}_before_run")
    for provider in status.get("providers", {}).values():
        if "last_success_before_run" in provider:
            provider["last_success_at"] = provider["last_success_before_run"]
    run.update({"ended_at": iso(now), "actions_result": "FAILURE",
                "collection_result": "FAILED", "current_stage": "PUBLISH_FAILED"})
    if run.get("started_at"):
        run["duration_seconds"] = round((now - utc(run["started_at"])).total_seconds(), 2)
    status["last_failure_at"] = iso(now)
    status["updated_at"] = iso(now)
    status["errors"] = (status.get("errors") or [])[:29] + [f"publish: {reason[:200]}"]
    _record_run(status)
    write_json_atomic(status, path, compact=True)
    return status


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("start", "stage", "finish", "publish-failed"))
    parser.add_argument("--output", type=Path, default=Path("data/system_status.json"))
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--stage", choices=sorted(STAGES))
    parser.add_argument("--slot", help="UTC collection slot, e.g. 2026-09-27T00:00:00Z")
    parser.add_argument("--run-mode", choices=("SCHEDULED", "MANUAL"))
    parser.add_argument("--trigger", choices=COLLECTION_TRIGGERS)
    parser.add_argument("--requested-at", help="GitHub Actions run creation time in UTC")
    parser.add_argument("--actions-result", default=os.getenv("ACTIONS_JOB_STATUS", "UNKNOWN"))
    parser.add_argument("--reason", default="Collection failed before final publish")
    args = parser.parse_args()
    if args.command == "start":
        if args.slot is not None:
            instant = utc(args.slot)
            if instant.minute or instant.second or instant.microsecond or instant.hour not in (0, 4, 8, 12, 16, 20):
                parser.error("--slot must be a four-hour UTC collection boundary")
        status = start(args.output, os.getenv("GITHUB_RUN_ID"), args.slot,
                       args.run_mode or "SCHEDULED", args.requested_at, args.trigger)
    elif args.command == "stage":
        if args.stage is None:
            parser.error("--stage is required")
        status = stage(args.output, args.stage)
    elif args.command == "publish-failed":
        status = publish_failed(args.output, args.reason, run_id=os.getenv("GITHUB_RUN_ID"),
                                run_mode=args.run_mode,
                                slot=args.slot, trigger=args.trigger,
                                requested_at=args.requested_at)
    else:
        status = finish(args.output, args.data_dir, args.actions_result,
                        run_id=os.getenv("GITHUB_RUN_ID"))
    print(json.dumps({"run": status["run"], "errors": status["errors"]},
                     separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
