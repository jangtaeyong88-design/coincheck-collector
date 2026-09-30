"""Decide whether a scheduled collection slot still needs a collector run.

Read only the latest remote main. GitHub Actions keeps all runs in one concurrency
group; this check prevents queued retries from recollecting a completed slot.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from collector.collection_quality import quality_result


SLOT_HOURS = (0, 4, 8, 12, 16, 20)
SLOT_LENGTH = timedelta(hours=4)
SCHEDULE = re.compile(r"^0,5,10,15 (0|4|8|12|16|20) \* \* \*$")
CORE_FILES = ("latest", "indicators", "market_summary", "derivatives_summary")


def _instant(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(UTC) if parsed.tzinfo else None
    except ValueError:
        return None


def slot_for_event(event_name: str, schedule: str | None, now: datetime) -> datetime | None:
    """Use the cron's UTC hour, not the possibly delayed runner start hour."""
    now = now.astimezone(UTC)
    if event_name == "workflow_dispatch":
        return None  # An intentional collection is not a retry of a scheduled slot.
    if event_name != "schedule":
        raise ValueError(f"Unsupported collection event: {event_name}")
    match = SCHEDULE.fullmatch(schedule or "")
    if match is None:
        raise ValueError(f"Unknown collection cron: {schedule}")
    hour = int(match.group(1))
    slot = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if slot > now:
        slot -= timedelta(days=1)
    return slot if now - slot < SLOT_LENGTH else None


def slot_id(slot: datetime) -> str:
    return slot.astimezone(UTC).isoformat().replace("+00:00", "Z")


def cloudflare_window_open(slot: datetime, now: datetime) -> bool:
    delay = now.astimezone(UTC) - slot.astimezone(UTC)
    return timedelta(0) <= delay <= timedelta(minutes=30)


def _same_slot(status: dict, slot: datetime) -> bool:
    run = status.get("run") or {}
    if run.get("run_mode") == "MANUAL":
        return False
    explicit = run.get("collection_slot")
    if explicit is not None:
        return _instant(explicit) == slot
    # One-time compatibility for runs published before collection_slot existed.
    started = _instant(run.get("started_at"))
    return started is not None and slot <= started < slot + SLOT_LENGTH


def slot_succeeded(status: dict | None, documents: dict[str, dict], slot: datetime) -> bool:
    if not isinstance(status, dict):
        return False
    # A later failed attempt cannot invalidate a fully recorded earlier success.
    if _recorded_slot_succeeded(status.get("last_scheduled_success"), slot):
        return True
    if not _same_slot(status, slot):
        return False
    run = status.get("run") or {}
    if run.get("actions_result") != "SUCCESS" or run.get("collection_result") not in \
            ("SUCCESS", "PARTIAL_ACCEPTABLE"):
        return False
    started, ended = _instant(run.get("started_at")), _instant(run.get("ended_at"))
    if started is None or ended is None or not slot <= started <= ended < slot + SLOT_LENGTH:
        return False
    last_success = _instant(status.get("last_success_at"))
    if last_success is None or last_success < ended:
        return False
    latest = documents.get("latest") or {}
    counts = latest.get("summary") or {}
    market_count = counts.get("market_count")
    if quality_result(counts, market_count) == "FAILED":
        return False
    for name in CORE_FILES:
        document = documents.get(name) or {}
        field = "generated_at" if name == "market_summary" else "collected_at"
        observed = _instant((document.get(field) or {}).get("utc"))
        if observed is None or observed < started or observed >= slot + SLOT_LENGTH:
            return False
    return _instant(status.get("data_collected_at")) == _instant(
        latest.get("collected_at", {}).get("utc"))


def _recorded_slot_succeeded(record: dict | None, slot: datetime) -> bool:
    """A later manual run can replace latest.json without erasing a scheduled success."""
    if not isinstance(record, dict) or record.get("run_mode") != "SCHEDULED" or \
            _instant(record.get("collection_slot")) != slot or \
            record.get("actions_result") != "SUCCESS" or \
            record.get("collection_result") not in ("SUCCESS", "PARTIAL_ACCEPTABLE") or \
            not isinstance(record.get("market_snapshot_id"), str) or \
            not re.fullmatch(r"sha256:[0-9a-f]{64}", record["market_snapshot_id"]):
        return False
    started, ended = _instant(record.get("started_at")), _instant(record.get("ended_at"))
    if started is None or ended is None or not slot <= started <= ended < slot + SLOT_LENGTH:
        return False
    upbit = record.get("upbit") or {}
    count = upbit.get("market_count")
    if quality_result({"market_count": count,
                       "successful_market_count": upbit.get("success_count"),
                       "failed_market_count": upbit.get("failure_count")}, count) == "FAILED":
        return False
    sources = record.get("source_collected_at") or {}
    for name in CORE_FILES:
        observed = _instant(sources.get(name))
        if observed is None or not started <= observed < slot + SLOT_LENGTH:
            return False
    return _instant(record.get("data_collected_at")) == _instant(sources.get("latest"))


@dataclass(frozen=True)
class Decision:
    collect: bool
    reason: str


def decide(slot: datetime, status: dict | None, documents: dict[str, dict],
           *, previous_run_active: bool = False, force_manual: bool = False) -> Decision:
    run = (status or {}).get("run") or {}
    if _same_slot(status or {}, slot) and run.get("collection_result") == "RUNNING" \
            and previous_run_active:
        return Decision(False, "SKIPPED_RUNNING")
    if not force_manual and slot_succeeded(status, documents, slot):
        return Decision(False, "SUCCESS_ALREADY_COLLECTED")
    return Decision(True, "COLLECT_REQUIRED")


def _git(repo: Path, *args: str) -> bytes:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True)
    if result.returncode:
        raise RuntimeError(f"git {' '.join(args)} failed: "
                           f"{result.stderr.decode(errors='replace').strip()}")
    return result.stdout


def _remote_json(repo: Path, revision: str, name: str) -> dict | None:
    result = subprocess.run(["git", "-C", str(repo), "show", f"{revision}:data/{name}.json"],
                            capture_output=True)
    if result.returncode:
        return None
    try:
        value = json.loads(result.stdout)
    except ValueError as error:
        raise RuntimeError(f"Remote data/{name}.json is not valid JSON") from error
    return value if isinstance(value, dict) else None


def _run_active(repository: str, run_id: str, token: str) -> bool:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Invalid GitHub repository name")
    if not run_id.isdecimal() or not token:
        raise ValueError("Cannot verify the previous RUNNING workflow")
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repository}/actions/runs/{run_id}",
        headers={"Accept": "application/vnd.github+json",
                 "Authorization": f"Bearer {token}",
                 "X-GitHub-Api-Version": "2022-11-28"},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        run = json.load(response)
    return run.get("status") != "completed"


def main() -> int:
    from scripts.collection_health import (append_summary, parse_recovery_slot, summary,
                                           workflow_runs)

    now = datetime.now(UTC)
    event_name = os.getenv("GITHUB_EVENT_NAME", "")
    requested_slot = os.getenv("COLLECTION_RECOVERY_SLOT", "").strip()
    manual_source = os.getenv("COLLECTION_MANUAL_SOURCE", "").strip()
    scheduled_source = os.getenv("COLLECTION_SCHEDULED_SOURCE", "").strip()
    if scheduled_source and (scheduled_source not in ("cloudflare_schedule", "cloudflare_retry") or
                             event_name != "workflow_dispatch" or not requested_slot or manual_source):
        raise ValueError("Invalid scheduled source")
    if manual_source and (manual_source != "android" or event_name != "workflow_dispatch" or requested_slot):
        raise ValueError("Invalid manual source")
    if requested_slot and event_name != "workflow_dispatch":
        raise ValueError("Recovery slot is only accepted for workflow_dispatch")
    slot = (parse_recovery_slot(requested_slot, now) if requested_slot else
            slot_for_event(event_name, os.getenv("GITHUB_EVENT_SCHEDULE"), now))
    if event_name == "schedule":
        # An already queued legacy cron event must not revive a past slot after
        # this workflow has moved to Cloudflare-only dispatch.
        decision = Decision(False, "SKIPPED_LEGACY_SCHEDULE")
    elif scheduled_source and not cloudflare_window_open(slot, now):
        decision = Decision(False, "SKIPPED_STALE_CLOUDFLARE_SLOT")
    else:
        repo = Path.cwd()
        _git(repo, "fetch", "--quiet", "origin", "main")
        revision = _git(repo, "rev-parse", "FETCH_HEAD").decode().strip()
        status = _remote_json(repo, revision, "system_status") or {}
        documents = {}
        if slot is not None and _same_slot(status, slot) and \
                (status.get("run") or {}).get("collection_result") in \
                ("SUCCESS", "PARTIAL_ACCEPTABLE"):
            documents = {name: _remote_json(repo, revision, name) or {}
                         for name in CORE_FILES}
        repository = os.getenv("GITHUB_REPOSITORY", "")
        token = os.getenv("GITHUB_TOKEN", "")
        current_id = os.getenv("GITHUB_RUN_ID", "")
        if slot is not None and slot_succeeded(status, documents, slot):
            # No new collector can start, so an Actions API outage need not turn
            # an already-published success into a failed retry workflow.
            runs = []
            decision = Decision(False, "SUCCESS_ALREADY_COLLECTED")
        else:
            try:
                runs = workflow_runs(repository, token)  # Fail closed if Actions cannot be checked.
            except (OSError, ValueError):
                append_summary("## Collection slot check\n\n- State: **UNKNOWN**\n"
                               "- Decision: **NO_COLLECTION**\n"
                               "- Reason: Actions run status could not be verified\n")
                raise
            other_running = [run for run in runs if str(run.get("id")) != current_id and
                             run.get("status") == "in_progress"]
            run = (status or {}).get("run") or {}
            if run.get("collection_result") == "RUNNING" and \
                    str(run.get("id") or "") != current_id and not other_running:
                if _run_active(repository, str(run.get("id") or ""), token):
                    other_running = [{"id": run.get("id")}]
            if other_running:
                decision = Decision(False, "SKIPPED_RUNNING")
            elif slot is None:
                decision = Decision(True, "COLLECT_REQUIRED")
            else:
                decision = decide(slot, status, documents)
        if slot is not None:
            from scripts.collection_health import SlotHealth, assess
            health = assess(slot, now, status, documents, runs, current_id)
            if decision.collect:
                health = SlotHealth(slot, "STARTING", "Collector preflight accepted this run")
            append_summary(summary(health, action=decision.reason))
    current_slot = slot_id(slot) if slot else ""
    print(f"Collection slot {current_slot or 'stale'}: {decision.reason}")
    output = os.getenv("GITHUB_OUTPUT")
    if output:
        with Path(output).open("a", encoding="utf-8") as handle:
            handle.write(f"collect={'true' if decision.collect else 'false'}\n")
            handle.write(f"slot={current_slot}\n")
            handle.write(f"run_mode={'MANUAL' if event_name == 'workflow_dispatch' and not requested_slot else 'SCHEDULED'}\n")
            trigger = ("CLOUDFLARE_SCHEDULE" if scheduled_source == "cloudflare_schedule" else
                       "CLOUDFLARE_RETRY" if scheduled_source == "cloudflare_retry" else
                       "RECOVERY" if requested_slot else "ANDROID_MANUAL" if manual_source == "android" else
                       "MANUAL" if event_name == "workflow_dispatch" else "SCHEDULE")
            handle.write(f"trigger={trigger}\n")
            handle.write(f"requested_at={slot_id(now)}\n")
            handle.write(f"reason={decision.reason}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
