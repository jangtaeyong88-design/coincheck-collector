# Public collection contract

The collector publishes these allowlisted JSON files when their stages produce them: `latest.json`, `indicators.json`, `market_summary.json`, `intraday_summary.json`, `derivatives_summary.json`, `derivatives_history.json`, `system_status.json`, `asset_identity_cache.json`, `identity_audit.json`, and `optional_provider_test.json`. Historical provider diagnostics may also use `derivatives_provider_test.json` or `identity_baseline.json`; they are not required for an initial run.

`system_status.json` separates the GitHub Actions outcome (`run.actions_result`) from collection quality (`run.collection_result`). `run.id`, `run.run_mode`, `run.trigger`, `run.collection_slot`, `run.requested_at`, `run.started_at`, `run.ended_at`, and `data_collected_at` provide provenance. A manual run has a null collection slot. Source observations have their own timestamps and missing-data states. The consumer must compare the published collector commit SHA and source times rather than treating a successful workflow as proof that all markets were collected.

This repository publishes only neutral collector-owned observations. Downstream consumers should pin one collector revision and inspect each source's observation time and missing-data state.

## Completed four-hour observations

Production invokes `collector.intraday --all-4h`. Every eligible KRW market with
a valid collected ticker is requested once from `/candles/minutes/240` (count 24).
Only completed candles are consumed. RVOL and relative traded value compare the
last completed candle with the preceding 20 contiguous completed four-hour candles.
Current/open candles, gaps and stale candles are never filled with invented observations.
The JSON records start/end observation times, requested/eligible/omitted counts,
and per-market support/failure reasons. The legacy hourly function remains callable
for compatibility, but the production workflow does not call it.

## Comparable futures turnover

`turnover_24h` is attached only to identity-verified markets from at most three bulk
operations: Bitget `mix/market/tickers.usdtVolume`, Gate USDT `futures/usdt/tickers.volume_24h_quote`,
and KuCoin `contracts/active.turnoverOf24h` for non-inverse USDT quote/settlement contracts.
It records USDT value, API path/field and request observation time. FETCH_OBSERVED_AT
is the fetch timestamp, not an invented exchange trade timestamp. Failed or ambiguous
values stay unavailable. `turnover_diagnostics` records operation/HTTP-attempt counts
and duration; existing provider request counts include these additional attempts.
Downstream comparison is limited to verified venues with available comparable values.
No quantity conversion or cross-venue sum is used as a substitute for trading volume.

The production derivatives path disables Binance/Bybit catalog and market requests.
New OI change output has only 4h and 24h keys. Raw per-provider observations and their
35-day history remain separate; existing 1h history observations are not deleted.
Unavailable past observations remain unknown. Optional-provider market limits remain unchanged.

Local official API checks confirmed completed BTC/ARX/DOS four-hour candle responses
and all three bulk turnover APIs. This is not a GitHub Actions production benchmark.
All-market requests replace the former 20-market rotation, adding up to one request
per newly covered ticker plus existing bounded retries. The next production run must
be checked for duration, per-market completeness and actual request counts.

References: [Bitget](https://www.bitget.com/docs/catalog/classic-contract-market/classic-contract-market),
[Gate](https://www.gate.com/docs/developers/apiv4/en/futures/),
[KuCoin](https://www.kucoin.com/docs-new/rest/futures-trading/market-data/get-all-symbols).
