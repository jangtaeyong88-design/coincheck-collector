"""Append actual collector outcome to GitHub's run summary without changing data."""

import json
import os
from pathlib import Path

from scripts.collection_health import append_summary
from scripts.collection_slot import _instant


def main() -> int:
    path = Path("data/system_status.json")
    if not path.is_file():
        append_summary("## Collector outcome\n\n- Status file unavailable\n")
        return 0
    status = json.loads(path.read_text(encoding="utf-8"))
    run = status.get("run") or {}
    expected = os.getenv("GITHUB_RUN_ID")
    if expected and str(run.get("id")) != expected:
        append_summary("## Collector outcome\n\n- No status was written for this workflow run. "
                       "The previous published status remains unchanged.\n")
        return 0
    lines = ["## Collector outcome", ""]
    for label, value in (("Run ID", run.get("id")), ("Trigger", run.get("trigger")),
                         ("Slot", run.get("collection_slot")),
                         ("Requested at", run.get("requested_at")),
                         ("Started at", run.get("started_at")),
                         ("Ended at", run.get("ended_at")),
                         ("Actions result", run.get("actions_result")),
                         ("Collection result", run.get("collection_result")),
                         ("Data observed at", status.get("data_collected_at")),
                         ("Duration seconds", run.get("duration_seconds")),
                         ("Published commit (data or failure status)", os.getenv("PUBLISHED_COMMIT"))):
        lines.append(f"- {label}: {value if value is not None and value != '' else 'N/A'}")
    current_requests = 0
    for name, provider in (status.get("providers") or {}).items():
        started, run_started = _instant(provider.get("started_at")), _instant(run.get("started_at"))
        is_current = started is not None and run_started is not None and started >= run_started
        if is_current and isinstance(provider.get("api_requests"), int):
            current_requests += provider["api_requests"]
        lines.append(f"- {name} ({'current run' if is_current else 'prior observation'}): "
                     f"{provider.get('status', 'N/A')}; "
                     f"requests={provider.get('api_requests', 'N/A')}; "
                     f"duration_seconds={provider.get('duration_seconds', 'N/A')}")
    lines.append(f"- Current provider API requests: {current_requests}")
    for error in status.get("errors") or []:
        lines.append(f"- Error: {str(error)[:200]}")
    append_summary("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
