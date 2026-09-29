"""Bounded, market-level collection tolerance shared by status and readiness."""

MIN_SUCCESS_RATE = 0.98
MAX_FAILED_MARKETS = 5


def quality_result(summary: dict, expected_count: int) -> str:
    """Reject malformed counts before deciding whether a partial run is usable."""
    if type(expected_count) is not int or expected_count <= 0 or not isinstance(summary, dict):
        return "FAILED"
    success = summary.get("successful_market_count")
    failed = summary.get("failed_market_count")
    if any(type(value) is not int for value in (summary.get("market_count"), success, failed)) or \
            summary.get("market_count") != expected_count or \
            success < 0 or failed < 0 or success + failed != expected_count:
        return "FAILED"
    if failed == 0:
        return "SUCCESS"
    if failed <= MAX_FAILED_MARKETS and success / expected_count >= MIN_SUCCESS_RATE:
        return "PARTIAL_ACCEPTABLE"
    return "FAILED"
