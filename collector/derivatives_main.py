"""Write compact data/derivatives_summary.json from Upbit and public futures APIs."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from collector.derivatives import build_derivatives_snapshot, default_clients
from collector.bitget_derivatives import BitgetClient, add_bitget
from collector.asset_identity import load_cache
from collector.derivatives_history import (add_observation_coverage, load_history,
                                           update_history, update_optional_history)
from collector.identity_audit import write_audit
from collector.optional_derivatives import add_optional_providers
from collector.main import write_json_atomic
from collector.system_status import provider_start, provider_finish


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--latest", type=Path, default=Path("data/latest.json"))
    parser.add_argument("--output", type=Path, default=Path("data/derivatives_summary.json"))
    parser.add_argument("--history", type=Path, default=Path("data/derivatives_history.json"))
    parser.add_argument("--market-summary", type=Path, default=Path("data/market_summary.json"))
    parser.add_argument("--optional-provider-test", type=Path, default=Path("data/optional_provider_test.json"))
    parser.add_argument("--identity-cache", type=Path, default=Path("data/asset_identity_cache.json"))
    parser.add_argument("--identity-baseline", type=Path, default=Path("data/identity_baseline.json"))
    parser.add_argument("--identity-audit", type=Path)
    args = parser.parse_args()
    publish_runtime = os.getenv("COINCHECK_PUBLISH_RUNTIME") == "1"
    status_path = Path("data/system_status.json")

    def transition(name: str, event: str, snapshot: dict | None = None) -> None:
        if not publish_runtime:
            return
        if event == "start":
            provider_start(status_path, name)
        else:
            provider_finish(status_path, name, snapshot or {})
        # Only system_status.json is published. The unfinished derivatives files remain local.
        from scripts.safe_publish import SafePublisher
        SafePublisher(Path.cwd()).publish("status")

    with args.latest.open(encoding="utf-8") as handle:
        latest = json.load(handle)
    binance, bybit = default_clients()
    result = build_derivatives_snapshot(latest, binance, bybit, legacy_providers=False)
    cache = load_cache(args.identity_cache)
    transition("bitget", "start")
    if cache:
        result = add_bitget(result, latest, BitgetClient(),
                            bitget_coins=cache.get("bitget_coins"),
                            references=cache.get("references"),
                            identity_cache=cache)
    else:
        result = add_bitget(result, latest, BitgetClient())
    transition("bitget", "finish", result)
    if args.optional_provider_test.exists():
        with args.optional_provider_test.open(encoding="utf-8") as handle:
            provider_test = json.load(handle)
        gate_ok = provider_test.get("gate", {}).get("connection_success") is True
        kucoin_ok = provider_test.get("kucoin", {}).get("connection_success") is True
        if gate_ok or kucoin_ok:
            market_summary = None
            if args.market_summary.exists():
                with args.market_summary.open(encoding="utf-8") as handle:
                    market_summary = json.load(handle)
            result = add_optional_providers(result, latest, market_summary,
                                            enable_gate=gate_ok, enable_kucoin=kucoin_ok,
                                            gate_currencies=cache.get("gate_currencies") if cache else None,
                                            kucoin_currencies=cache.get("kucoin_currencies") if cache else None,
                                            references=cache.get("references") if cache else None,
                                            identity_cache=cache,
                                            on_provider_start=lambda name: transition(name, "start"),
                                            on_provider_finish=lambda name, snapshot: transition(name, "finish", snapshot))
        result["optional_provider_probe"] = {
            "checked_at_utc": provider_test.get("checked_at_utc"),
            "gate_enabled": gate_ok, "kucoin_enabled": kucoin_ok}
    history = update_history(load_history(args.history), result)
    history = update_optional_history(history, result)
    add_observation_coverage(result)
    from collector.market_turnover import add_turnover
    add_turnover(result)
    for pair in result["markets"].values():
        for row in pair.values():
            ((row.get("oi") or {}).get("changes") or {}).pop("1h", None)
    for counts in result["summary"].get("provider_counts", {}).values():
        counts.get("oi_change_available", {}).pop("1h", None)
    write_json_atomic(history, args.history, compact=True)
    write_json_atomic(result, args.output, compact=True)
    identity_audit = write_audit(result, latest, cache, args.identity_baseline,
                                 args.identity_audit or args.output.with_name("identity_audit.json"))
    print("Identity audit:", {provider: {
        "newly_verified": len(info["newly_verified"]),
        "still_unverified": len(info["still_unverified"])}
        for provider, info in identity_audit["providers"].items()})
    summary = result["summary"]
    print(f"Wrote {summary['market_count']} KRW markets, "
          f"{summary['matched_market_count']} matched, "
          f"{summary['failed_market_count']} with fetch failures to {args.output}")
    print(f"Bitget: {summary.get('provider_counts', {}).get('bitget', {})}; "
          f"OI observations retained: {history['summary']['oi_observation_count']} "
          f"in {args.history}")
    for provider in ("gate", "kucoin"):
        if provider in summary.get("provider_counts", {}):
            print(f"{provider}: " + json.dumps(summary["provider_counts"][provider],
                                               ensure_ascii=False, separators=(",", ":")))
    for exchange, details in result["exchange_diagnostics"].items():
        print(f"{exchange} catalog failure: "
              f"{json.dumps(details, ensure_ascii=False, separators=(',', ':'))}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
