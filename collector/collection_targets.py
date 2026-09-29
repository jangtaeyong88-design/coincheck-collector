"""Neutral coverage targets derived only from public Upbit observations."""

from __future__ import annotations

import math


def eligible_markets(latest: dict) -> set[str]:
    """A bounded provider collector may query any market with a valid Upbit ticker."""
    result = set()
    for row in latest.get("markets") or []:
        if not isinstance(row, dict):
            continue
        code = row.get("market")
        ticker = row.get("ticker") or {}
        price = ticker.get("trade_price")
        if (isinstance(code, str) and code.startswith("KRW-") and
                isinstance(price, (int, float)) and not isinstance(price, bool) and
                math.isfinite(price) and price > 0):
            result.add(code)
    return result


def rotating_markets(latest: dict, limit: int, rotation: int) -> tuple[list[str], int]:
    """Rotate fairly through observable markets without an investment ranking."""
    if limit < 1:
        raise ValueError("Market limit must be positive")
    markets = sorted(eligible_markets(latest))
    if not markets:
        return [], 0
    offset = rotation * limit % len(markets)
    ordered = markets[offset:] + markets[:offset]
    return ordered[:limit], max(0, len(markets) - limit)
