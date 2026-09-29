"""Validate collector-owned JSON without requiring data before the first run."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from scripts.public_repo_guard import check_json


CORE = ("latest", "indicators", "market_summary", "derivatives_summary")


def validate(root: Path) -> dict[str, dict]:
    data = root / "data"
    documents: dict[str, dict] = {}
    for path in data.glob("*.json"):
        relative = f"data/{path.name}"
        raw = path.read_bytes()
        check_json(relative, raw)
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError(f"Public JSON root must be an object: {relative}")
        documents[path.stem] = value
    status = documents.get("system_status")
    if status is None:
        return documents
    run = status.get("run") or {}
    if not isinstance(run, dict):
        raise ValueError("Invalid system_status run")
    if run.get("actions_result") != "SUCCESS" or run.get("collection_result") not in \
            ("SUCCESS", "PARTIAL_ACCEPTABLE"):
        return documents  # A published in-progress or failed status may precede a new snapshot.
    if not run.get("id") or not run.get("started_at") or not run.get("ended_at"):
        raise ValueError("Successful run lacks provenance")
    missing = set(CORE) - documents.keys()
    if missing:
        raise ValueError(f"Successful run lacks core JSON: {', '.join(sorted(missing))}")
    latest = documents["latest"]
    observed = (latest.get("collected_at") or {}).get("utc")
    counts = latest.get("summary") or {}
    total = counts.get("market_count")
    succeeded = counts.get("successful_market_count")
    failed = counts.get("failed_market_count")
    if not observed or status.get("data_collected_at") != observed or \
            not all(isinstance(n, int) and not isinstance(n, bool) for n in
                    (total, succeeded, failed)) or total <= 0 or \
            succeeded < 0 or failed < 0 or succeeded + failed != total:
        raise ValueError("Successful run has inconsistent Upbit snapshot")
    for name in CORE[1:]:
        timestamp = (documents[name].get("generated_at") if name == "market_summary"
                     else documents[name].get("collected_at")) or {}
        if not isinstance(timestamp, dict) or not timestamp.get("utc"):
            raise ValueError(f"Successful run lacks {name} source time")
    scheduled = status.get("last_scheduled_success") or {}
    if str(scheduled.get("collection_run_id")) == str(run["id"]) and \
            scheduled.get("data_collected_at") == observed:
        digest = "sha256:" + hashlib.sha256((data / "latest.json").read_bytes()).hexdigest()
        if scheduled.get("market_snapshot_id") != digest:
            raise ValueError("Scheduled run latest snapshot digest mismatch")
    return documents


if __name__ == "__main__":
    documents = validate(Path.cwd())
    print(f"Public data validation passed ({len(documents)} JSON files)")
