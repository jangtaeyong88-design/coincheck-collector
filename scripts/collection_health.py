"""Read-only diagnosis of the current collection slot and its Actions runs.

The published system status describes the last run, not whether GitHub created
an event for the current slot. A missing event cannot update that JSON file.
"""

from __future__ import annotations

import json
import os
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from scripts.collection_slot import SLOT_HOURS, SLOT_LENGTH, _instant, slot_id, slot_succeeded

WORKFLOW = "collect.yml"
ACTIVE_STATES = {"queued", "pending", "waiting", "requested", "in_progress"}
RECOVERY_MINUTE = 16  # The normal :00/:05/:10/:15 attempts have had their chance.


def current_slot(now: datetime) -> datetime:
    now = now.astimezone(UTC)
    hour = max((hour for hour in SLOT_HOURS if hour <= now.hour), default=SLOT_HOURS[-1])
    day = now.date() if hour <= now.hour else (now - timedelta(days=1)).date()
    return datetime(day.year, day.month, day.day, hour, tzinfo=UTC)


def parse_recovery_slot(value: str, now: datetime) -> datetime:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:00:00Z", value):
        raise ValueError("Recovery slot must be an exact UTC four-hour boundary")
    slot = _instant(value)
    if slot is None or slot.hour not in SLOT_HOURS or slot != current_slot(now):
        raise ValueError("Only the current collection slot can be recovered")
    if not slot <= now.astimezone(UTC) < slot + SLOT_LENGTH:
        raise ValueError("Recovery slot is outside its collection window")
    return slot


def workflow_runs(repository: str, token: str) -> list[dict]:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) or not token:
        raise ValueError("Actions run verification requires repository and token")
    url = f"https://api.github.com/repos/{repository}/actions/workflows/{WORKFLOW}/runs?per_page=100"
    request = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "coincheck-slot-health"})
    with urllib.request.urlopen(request, timeout=15) as response:
        payload = json.load(response)
    if not isinstance(payload, dict) or not isinstance(payload.get("workflow_runs"), list):
        raise ValueError("Invalid Actions runs response")
    return payload["workflow_runs"]


@dataclass(frozen=True)
class SlotHealth:
    slot: datetime
    state: str
    reason: str
    related_run_ids: tuple[str, ...] = ()


def assess(slot: datetime, now: datetime, status: dict | None,
           documents: dict[str, dict], runs: list[dict], current_run_id: str = "") -> SlotHealth:
    if slot_succeeded(status, documents, slot):
        return SlotHealth(slot, "SUCCESS", "Published core data and slot status verified")
    active = [run for run in runs if str(run.get("id")) != current_run_id and
              run.get("status") in ACTIVE_STATES]
    if active:
        ids = tuple(str(run["id"]) for run in active)
        state = "RUNNING" if any(run.get("status") == "in_progress" for run in active) else "QUEUED"
        return SlotHealth(slot, state, "Another collection workflow is active", ids)
    if now.astimezone(UTC) < slot + timedelta(minutes=RECOVERY_MINUTE):
        return SlotHealth(slot, "WAITING", "Normal scheduled attempts have not all elapsed")
    recent = [run for run in runs if (_instant(run.get("created_at")) or slot - SLOT_LENGTH) >= slot
              and str(run.get("id")) != current_run_id]
    if not recent:
        return SlotHealth(slot, "NO_RUN_VISIBLE", "No collection workflow run is visible for this slot")
    ids = tuple(str(run["id"]) for run in recent)
    if all(run.get("conclusion") in ("failure", "cancelled", "timed_out") for run in recent):
        return SlotHealth(slot, "FAILED", "Recent workflow runs did not complete successfully", ids)
    return SlotHealth(slot, "UNVERIFIED_COMPLETION",
                      "A workflow completed, but published slot data is not verified", ids)


def summary(health: SlotHealth, *, action: str) -> str:
    return "\n".join((
        "## Collection slot check", "",
        f"- Target slot (UTC): `{slot_id(health.slot)}`",
        f"- State: **{health.state}**",
        f"- Reason: {health.reason}",
        f"- Related run IDs: {', '.join(health.related_run_ids) or 'none'}",
        f"- Decision: **{action}**", ""))


def append_summary(content: str) -> None:
    path = os.getenv("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(content)
    print(content)
