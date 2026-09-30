"""Command-line entry point for collecting the latest Upbit snapshot."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from collector.upbit import UpbitAPIError, UpbitClient, collect_market_data


KST = timezone(timedelta(hours=9), name="KST")


def build_snapshot(client: UpbitClient) -> dict[str, Any]:
    started_at = datetime.now(UTC)
    collected = collect_market_data(client)
    records = collected["markets"]
    failed = sum(bool(record["errors"]) for record in records)
    return {
        "schema_version": "1.0",
        "source": "Upbit public API",
        "collected_at": {
            "utc": started_at.isoformat().replace("+00:00", "Z"),
            "kst": started_at.astimezone(KST).isoformat(),
        },
        "summary": {
            "market_count": collected["market_count"],
            "successful_market_count": collected["market_count"] - failed,
            "failed_market_count": failed,
        },
        "markets": records,
    }


def write_json_atomic(
    snapshot: dict[str, Any], output: Path, *, compact: bool = False
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(dir=output.parent, prefix=f".{output.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(
                snapshot,
                handle,
                ensure_ascii=False,
                indent=None if compact else 2,
                separators=(",", ":") if compact else None,
            )
            handle.write("\n")
        os.replace(temporary_name, output)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("data/latest.json"))
    args = parser.parse_args()
    try:
        snapshot = build_snapshot(UpbitClient())
    except UpbitAPIError as exc:
        parser.error(f"could not obtain the KRW market list: {exc}")
    write_json_atomic(snapshot, args.output)
    print(
        f"Wrote {snapshot['summary']['market_count']} markets "
        f"({snapshot['summary']['failed_market_count']} partial failures) to {args.output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
