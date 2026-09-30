"""Write data/market_summary.json from existing latest and indicators files."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from collector.main import write_json_atomic
from collector.market_summary import build_market_summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--latest", type=Path, default=Path("data/latest.json"))
    parser.add_argument("--indicators", type=Path, default=Path("data/indicators.json"))
    parser.add_argument("--output", type=Path, default=Path("data/market_summary.json"))
    args = parser.parse_args()

    with args.latest.open(encoding="utf-8") as handle:
        latest = json.load(handle)
    with args.indicators.open(encoding="utf-8") as handle:
        indicators = json.load(handle)
    summary = build_market_summary(latest, indicators)
    write_json_atomic(summary, args.output, compact=True)
    counts = summary["summary"]
    print(
        f"Wrote {counts['market_count']} markets "
        f"({counts['failed_market_count']} failures, "
        f"{counts['insufficient_data_market_count']} insufficient) to {args.output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
