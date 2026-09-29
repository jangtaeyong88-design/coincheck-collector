# Public repository boundary

Only explicitly allowlisted collector files and neutral runtime JSON belong in this repository. `python -m scripts.public_repo_guard` checks the complete working tree for forbidden paths, sensitive content, and unsafe JSON keys. The security workflow also scans the complete Git history and working tree with a checksum-verified Gitleaks binary.

This repository has fresh Git history. It does not inherit a private repository commit or require access to private data. Fork pull requests receive no production secret. Collection runs only through `workflow_dispatch` on `main` and publishes collector-owned data. Review both the security checks and the data contract before enabling an external scheduler.
