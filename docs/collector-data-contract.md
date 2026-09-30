# Public collection contract

The collector publishes these allowlisted JSON files when their stages produce them: `latest.json`, `indicators.json`, `market_summary.json`, `intraday_summary.json`, `derivatives_summary.json`, `derivatives_history.json`, `system_status.json`, `asset_identity_cache.json`, `identity_audit.json`, and `optional_provider_test.json`. Historical provider diagnostics may also use `derivatives_provider_test.json` or `identity_baseline.json`; they are not required for an initial run.

`system_status.json` separates the GitHub Actions outcome (`run.actions_result`) from collection quality (`run.collection_result`). `run.id`, `run.run_mode`, `run.trigger`, `run.collection_slot`, `run.requested_at`, `run.started_at`, `run.ended_at`, and `data_collected_at` provide provenance. A manual run has a null collection slot. Source observations have their own timestamps and missing-data states. The consumer must compare the published collector commit SHA and source times rather than treating a successful workflow as proof that all markets were collected.

This repository publishes only neutral collector-owned observations. Downstream consumers should pin one collector revision and inspect each source's observation time and missing-data state.
