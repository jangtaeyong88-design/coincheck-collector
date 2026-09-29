"""Small, read-only BTC/ETH/SOL probe for the GitHub Actions runner network."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from collector.derivatives import (
    DerivativesAPIError, PublicClient, _catalog, build_derivatives_snapshot,
    default_clients,
)


def probe(latest: dict, *, check_alternate: bool = True) -> tuple[list[dict], bool]:
    targets = {"KRW-BTC", "KRW-ETH", "KRW-SOL"}
    rows = [row for row in latest.get("markets", []) if row.get("market") in targets]
    missing = sorted(targets - {row["market"] for row in rows})
    events: list[dict] = [{"event": "source_missing", "market": code} for code in missing]
    if not rows:
        return events, False
    binance, bybit = default_clients()
    snapshot = build_derivatives_snapshot({"markets": rows}, binance, bybit)
    for exchange in ("binance", "bybit"):
        if exchange in snapshot["exchange_diagnostics"]:
            events.append({"event": "catalog_failure", "exchange": exchange,
                           **snapshot["exchange_diagnostics"][exchange]})
        else:
            events.append({"event": "catalog_success", "exchange": exchange})
    success = not missing
    for code in sorted(targets - set(missing)):
        for exchange in ("binance", "bybit"):
            record = snapshot["markets"][code][exchange]
            oi = record["oi"] or {}
            funding = record["funding"] or {}
            verified = record["match"]["status"] == "VERIFIED"
            oi_available = verified and oi.get("exchange_quantity") is not None
            funding_available = verified and funding.get("latest_rate") is not None
            status = ("SUCCESS" if oi_available and funding_available else
                      "PARTIAL" if oi_available or funding_available else "FAILED")
            success &= status == "SUCCESS"
            events.append({"event": "market_probe", "market": code, "exchange": exchange,
                           "status": status,
                           "match_status": record["match"]["status"],
                           "symbol": record["match"].get("symbol"),
                           "oi_status": "AVAILABLE" if oi_available else "UNAVAILABLE",
                           "oi_quantity": oi.get("exchange_quantity"),
                           "oi_at_ms": oi.get("at_ms"),
                           "funding_status": "AVAILABLE" if funding_available else "UNAVAILABLE",
                           "funding_rate": funding.get("latest_rate"),
                           "errors": record["errors"]})
    # Bytick is a documented Bybit mainnet domain. Probe it separately for
    # diagnostics, but never switch collection after a 403/451 denial.
    if check_alternate:
        alternate = PublicClient("https://api.bytick.com", attempts=1, timeout=8)
        try:
            instruments = _catalog(alternate, "bybit")
            events.append({"event": "official_alternate_probe", "exchange": "bybit",
                           "host": "api.bytick.com", "status": "REACHABLE",
                           "instrument_count": len(instruments), "used_for_collection": False})
        except DerivativesAPIError as exc:
            events.append({"event": "official_alternate_probe", "exchange": "bybit",
                           "host": "api.bytick.com", "status": "FAILED",
                           "details": exc.details or {"message": str(exc)},
                           "used_for_collection": False})
    return events, success


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--latest", type=Path, default=Path("data/latest.json"))
    parser.add_argument("--strict", action="store_true",
                        help="fail the Actions check when any BTC/ETH/SOL OI is unavailable")
    args = parser.parse_args()
    with args.latest.open(encoding="utf-8") as handle:
        latest = json.load(handle)
    events, success = probe(latest)
    for event in events:
        print(json.dumps(event, ensure_ascii=False, separators=(",", ":")), flush=True)
    return 1 if args.strict and not success else 0


if __name__ == "__main__":
    raise SystemExit(main())
