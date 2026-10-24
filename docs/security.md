# Public repository boundary

Only explicitly allowlisted collector files and neutral runtime JSON belong in this repository. `python -m scripts.public_repo_guard` checks the complete working tree for forbidden paths, sensitive content, and unsafe JSON keys. The security workflow also scans the complete Git history and working tree with a checksum-verified Gitleaks binary.

This repository has fresh Git history. It does not inherit a private repository commit or require access to private data. Fork pull requests receive no production secret. Collection runs only through `workflow_dispatch` on `main` and publishes collector-owned data. Review both the security checks and the data contract before enabling an external scheduler.

Gitleaks defaults remain enabled. Its generic-key detector also matched fourteen
public contract addresses whose CoinGecko platform keys were huobi-token, klay-token,
hashkey-chain or secret. The checked-in exception applies only to this rule, only
to `data/asset_identity_cache.json`, and only to these platform keys followed by a
40-hex EVM address or a Secret Network public address. It does not exempt the file,
credential-shaped values, other rules, or those same strings in source files.
Production cache contents are not rewritten to satisfy the scan.
