"""Collect historical Upbit candles and write data/indicators.json."""

from __future__ import annotations

import argparse
from pathlib import Path

from collector.indicators import build_indicators_snapshot
from collector.main import write_json_atomic
from collector.upbit import UpbitAPIError, UpbitClient


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("data/indicators.json"))
    parser.add_argument(
        "--minute-days", type=int, choices=range(0, 4), default=0,
        help="optionally collect 1-3 days of minute candles (default: disabled)",
    )
    parser.add_argument(
        "--minute-unit", type=int, choices=(1, 3, 5, 10, 15, 30, 60, 240), default=60
    )
    args = parser.parse_args()
    try:
        snapshot = build_indicators_snapshot(
            UpbitClient(), minute_days=args.minute_days, minute_unit=args.minute_unit
        )
    except UpbitAPIError as exc:
        parser.error(f"could not obtain the KRW market list: {exc}")
    write_json_atomic(snapshot, args.output)
    summary = snapshot["summary"]
    print(
        f"Wrote {summary['market_count']} markets "
        f"({summary['failed_market_count']} failures, "
        f"{summary['insufficient_data_market_count']} insufficient) to {args.output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
