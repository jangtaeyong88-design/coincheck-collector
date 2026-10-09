"""Comparable quote turnover observations; no cross-provider quantity sums."""
from datetime import UTC, datetime
from time import monotonic
from collector.bitget_derivatives import BitgetClient
from collector.derivatives import DerivativesAPIError, _number
from collector.optional_derivatives import MeteredClient, GATE_HOST, KUCOIN_HOST


def add_turnover(snapshot, *, clients=None, clock=None):
    clock = clock or (lambda: datetime.now(UTC))
    clients = clients or {"bitget": BitgetClient(), "gate": MeteredClient(GATE_HOST),
                          "kucoin": MeteredClient(KUCOIN_HOST)}
    specs = {"bitget": ("/api/v2/mix/market/tickers", {"productType": "USDT-FUTURES"}, "symbol", "usdtVolume"),
             "gate": ("/futures/usdt/tickers", None, "contract", "volume_24h_quote"),
             "kucoin": ("/api/v1/contracts/active", None, "symbol", "turnoverOf24h")}
    diagnostics = {}
    for provider, (path, params, symbol_key, field) in specs.items():
        records = [pair[provider] for pair in snapshot["markets"].values()
                   if provider in pair and pair[provider].get("match", {}).get("status") == "VERIFIED"]
        if not records:
            continue
        started = monotonic()
        try:
            payload = clients[provider].get(path, params)
            rows = payload.get("data") if isinstance(payload, dict) else payload
            if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
                raise ValueError("Invalid ticker list")
            by_symbol = {}
            for row in rows:
                symbol = row.get(symbol_key)
                if symbol in by_symbol:
                    raise ValueError("Duplicate ticker")
                by_symbol[symbol] = row
            observed = clock().isoformat().replace("+00:00", "Z")
            for record in records:
                row = by_symbol.get(record["match"]["symbol"], {})
                value = _number(row.get(field))
                # Do not reinterpret inverse, coin-margined or non-USDT contracts.
                compatible = provider != "kucoin" or (row.get("quoteCurrency") == "USDT" and
                    row.get("settleCurrency") == "USDT" and row.get("isInverse") is False)
                valid = compatible and value is not None and value >= 0
                record["turnover_24h"] = {"status": "AVAILABLE" if valid else "NOT_SUPPORTED",
                    "value": value if valid else None, "currency": "USDT",
                    "observed_at": observed, "timestamp_kind": "FETCH_OBSERVED_AT",
                    "source": path, "source_field": field}
            diagnostics[provider] = {"status": "AVAILABLE", "api_operation_count": 1,
                "http_attempt_count": getattr(clients[provider], "http_attempt_count", None)}
        except (DerivativesAPIError, ValueError, KeyError, TypeError):
            diagnostics[provider] = {"status": "FETCH_FAILED", "path": path, "api_operation_count": 1}
            for record in records:
                record["turnover_24h"] = {"status": "FETCH_FAILED", "value": None, "currency": "USDT"}
        diagnostics[provider]["duration_seconds"] = round(monotonic() - started, 3)
        attempts = getattr(clients[provider], "http_attempt_count", None)
        diagnostics[provider]["http_attempt_count"] = attempts
        counts = snapshot.get("summary", {}).get("provider_counts", {}).get(provider)
        if counts is not None:
            if isinstance(counts.get("request_count"), int) and isinstance(attempts, int):
                counts["request_count"] += attempts
            if isinstance(counts.get("api_operation_count"), int):
                counts["api_operation_count"] += 1
    snapshot["turnover_diagnostics"] = diagnostics
    return snapshot
