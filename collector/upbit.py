"""Client and collector for Upbit's public quotation API."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, build_opener


API_BASE_URL = "https://api.upbit.com/v1"


class UpbitAPIError(RuntimeError):
    """Raised after an Upbit request exhausts all retry attempts."""


@dataclass(frozen=True)
class RetryConfig:
    attempts: int = 4
    backoff_seconds: float = 1.0
    timeout_seconds: float = 10.0


class UpbitClient:
    """Small, reusable client for Upbit public endpoints."""

    def __init__(
        self,
        opener: Any | None = None,
        retry: RetryConfig | None = None,
        request_interval_seconds: float = 0.12,
    ) -> None:
        self.opener = opener or build_opener()
        self.retry = retry or RetryConfig()
        self.request_interval_seconds = request_interval_seconds

    def _get(self, path: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        last_error = "unknown error"
        for attempt in range(1, self.retry.attempts + 1):
            try:
                query = f"?{urlencode(params)}" if params else ""
                request = Request(
                    f"{API_BASE_URL}{path}{query}",
                    headers={"Accept": "application/json"},
                )
                with self.opener.open(request, timeout=self.retry.timeout_seconds) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                if not isinstance(payload, list) or not payload:
                    raise ValueError("API returned an empty or invalid response")
                time.sleep(self.request_interval_seconds)
                return payload
            except (HTTPError, URLError, TimeoutError, ValueError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt < self.retry.attempts:
                    time.sleep(self.retry.backoff_seconds * (2 ** (attempt - 1)))
        raise UpbitAPIError(
            f"GET {path} failed after {self.retry.attempts} attempts: {last_error}"
        )

    def get_krw_markets(self) -> list[dict[str, Any]]:
        markets = self._get("/market/all", {"is_details": "true"})
        return sorted(
            (item for item in markets if str(item.get("market", "")).startswith("KRW-")),
            key=lambda item: str(item["market"]),
        )

    def get_tickers(self, markets: Iterable[str]) -> list[dict[str, Any]]:
        market_list = list(markets)
        if not market_list:
            return []
        return self._get("/ticker", {"markets": ",".join(market_list)})

    def get_daily_candle(self, market: str) -> dict[str, Any]:
        return self._get("/candles/days", {"market": market, "count": 1})[0]

    def get_daily_candles(self, market: str, count: int = 60) -> list[dict[str, Any]]:
        """Return up to 200 daily candles, newest first."""
        if not 1 <= count <= 200:
            raise ValueError("daily candle count must be between 1 and 200")
        return self._get("/candles/days", {"market": market, "count": count})

    def get_minute_candles(
        self, market: str, unit: int = 60, count: int = 24, to: str | None = None
    ) -> list[dict[str, Any]]:
        """Return optional minute candles, newest first."""
        supported_units = {1, 3, 5, 10, 15, 30, 60, 240}
        if unit not in supported_units:
            raise ValueError(f"minute unit must be one of {sorted(supported_units)}")
        if not 1 <= count <= 200:
            raise ValueError("minute candle count must be between 1 and 200")
        params: dict[str, Any] = {"market": market, "count": count}
        if to is not None:
            params["to"] = to
        return self._get(f"/candles/minutes/{unit}", params)


def chunks(values: list[str], size: int) -> Iterable[list[str]]:
    """Yield fixed-size API request batches."""
    for index in range(0, len(values), size):
        yield values[index : index + size]


def collect_market_data(client: UpbitClient) -> dict[str, Any]:
    """Collect ticker and daily candle data, retaining per-market failures."""
    market_details = client.get_krw_markets()
    market_codes = [str(item["market"]) for item in market_details]
    detail_by_code = {str(item["market"]): item for item in market_details}
    tickers: dict[str, dict[str, Any]] = {}
    errors: dict[str, list[dict[str, str]]] = {}

    # Upbit accepts at most 100 ticker codes. If a batch fails, retry each code
    # independently so one bad market cannot discard the rest of the batch.
    for batch in chunks(market_codes, 100):
        try:
            tickers.update({str(item["market"]): item for item in client.get_tickers(batch)})
        except UpbitAPIError:
            for code in batch:
                try:
                    ticker = client.get_tickers([code])[0]
                    tickers[code] = ticker
                except (UpbitAPIError, IndexError, KeyError) as exc:
                    errors.setdefault(code, []).append(
                        {"stage": "ticker", "reason": str(exc)}
                    )

    records: list[dict[str, Any]] = []
    for code in market_codes:
        candle = None
        try:
            candle = client.get_daily_candle(code)
        except (UpbitAPIError, IndexError, KeyError) as exc:
            errors.setdefault(code, []).append({"stage": "daily_candle", "reason": str(exc)})

        if code not in tickers and not any(e["stage"] == "ticker" for e in errors.get(code, [])):
            errors.setdefault(code, []).append(
                {"stage": "ticker", "reason": "market was absent from API response"}
            )
        records.append(
            {
                "market": code,
                "metadata": detail_by_code[code],
                "ticker": tickers.get(code),
                "daily_candle": candle,
                "errors": errors.get(code, []),
            }
        )

    return {"markets": records, "market_count": len(market_codes)}
