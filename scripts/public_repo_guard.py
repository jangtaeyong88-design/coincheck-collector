"""Fail closed when a public collector tree or output JSON crosses its allowlist."""

from __future__ import annotations

import json
import re
from pathlib import Path


ROOT_FILES = {"README.md", ".gitignore", "requirements.txt", "data/.gitkeep"}
COLLECTOR_MODULES = {
    "__init__", "main", "upbit", "indicators", "indicators_main",
    "market_summary", "market_summary_main", "intraday", "collection_targets",
    "collection_quality", "system_status", "derivatives", "derivatives_main",
    "derivatives_diagnostic", "derivatives_history", "bitget_derivatives",
    "bitget_identity", "optional_derivatives", "optional_provider_probe",
    "provider_probe", "asset_identity", "identity_audit", "time_utils",
}
SCRIPT_MODULES = {
    "public_repo_guard", "collection_slot", "collection_health",
    "collection_result_summary", "safe_publish", "validate_public_data",
}
TEST_MODULES = {
    "test_public_boundaries", "test_bitget_derivatives", "test_bitget_identity",
    "test_collection_slot", "test_collector", "test_derivatives",
    "test_derivatives_history", "test_derivatives_integration", "test_indicators",
    "test_intraday", "test_market_summary", "test_optional_derivatives",
    "test_provider_probe", "test_runtime_status", "test_upbit_integration",
    "test_collection_health", "test_safe_publish", "test_asset_identity",
}
RUNTIME_NAMES = {
    "latest", "indicators", "market_summary", "intraday_summary",
    "derivatives_summary", "derivatives_history", "system_status",
    "asset_identity_cache", "identity_audit", "identity_baseline",
    "optional_provider_test", "derivatives_provider_test",
}
DOCS = {"security.md", "collector-data-contract.md"}
WORKFLOWS = {"security.yml", "test.yml", "collect.yml"}
SENSITIVE_KEYS = {
    "password", "private_key", "access_token", "refresh_token", "authorization",
    "user_id", "device_id", "analysis_text", "prompt", "recommendation",
    "tracking", "token", "secret", "project_score", "pump_setup_score",
    "trigger_score", "proximity_score", "mfe", "mae",
}
SECRET_PATTERNS = (
    re.compile("gh" + r"p_[A-Za-z0-9]{20,}"),
    re.compile("github" + r"_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"Authorization:\s*Bearer\s+[A-Za-z0-9._~+/-]{20,}", re.I),
    re.compile(r"[A-Za-z]:" + r"\\Users\\", re.I),
)
STRATEGY_PATTERNS = tuple(re.compile(value, re.I) for value in (
    r"\bTOP[123]\b", r"Best\s+Watch", r"PRE_PUMP_WATCH",
    r"IGNITION_WATCH", r"\b(?:project|pump[_\s-]*setup|trigger|proximity)[_\s-]*score\b",
    r"\b(?:mfe|mae)(?:_[a-z0-9]+)?\b", r"\b(?:max_favorable|max_adverse)_excursion\b",
    r"\b(?:mfe|mae)(?:Pct|Percent|Price|At|Value)\b",
    r"\b(?:partial|coverage|confidence)[_\s-]*score\b",
    r"\b(?:candidate_pool|learning_loop|project_360)\b", r"interest_price",
    r"invalidation", r"coin_tracking", r"tracking_days",
    r"recommendation\s+episode", r"prepump_screening", r"app_settings",
    r"coincheck_reports",
))
# These files name forbidden concepts to document or test the boundary itself.
STRATEGY_EXEMPT = {
    "README.md", "docs/security.md", "docs/collector-data-contract.md",
    "scripts/public_repo_guard.py", "tests/test_public_boundaries.py",
}
SKIP_DIRECTORIES = {".git", "__pycache__", ".pytest_cache"}


def allowed_path(relative: str) -> bool:
    path = Path(relative)
    parts = path.parts
    if relative in ROOT_FILES or relative == ".github/CODEOWNERS":
        return True
    if len(parts) == 2 and parts[0] == "collector":
        return path.suffix == ".py" and path.stem in COLLECTOR_MODULES
    if len(parts) == 2 and parts[0] == "scripts":
        return path.suffix == ".py" and path.stem in SCRIPT_MODULES
    if len(parts) == 2 and parts[0] == "tests":
        return path.suffix == ".py" and path.stem in TEST_MODULES
    if len(parts) == 2 and parts[0] == "docs":
        return path.name in DOCS
    if len(parts) == 2 and parts[0] == "data":
        return path.suffix == ".json" and path.stem in RUNTIME_NAMES
    if len(parts) == 3 and parts[:2] == (".github", "workflows"):
        return path.name in WORKFLOWS
    return False


def _scan_text(text: str, relative: str) -> None:
    for pattern in SECRET_PATTERNS:
        if pattern.search(text):
            raise ValueError(f"Possible credential or local path in {relative}")
    if relative not in STRATEGY_EXEMPT:
        for pattern in STRATEGY_PATTERNS:
            if pattern.search(text):
                raise ValueError(f"Private strategy term in {relative}: {pattern.pattern}")
    for match in re.finditer(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text):
        if not match.group().lower().endswith(("@users.noreply.github.com", "@example.com")):
            raise ValueError(f"Personal email in {relative}")


def _scan_json_keys(value: object, relative: str, path: tuple[str, ...] = ()) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise ValueError(f"Non-string JSON key in {relative}")
            public_chain = (relative == "data/asset_identity_cache.json" and
                            len(path) == 3 and path[0] == "references" and
                            path[2] == "platforms" and key == "secret" and
                            isinstance(child, str) and child.startswith("secret1"))
            if key.lower() in SENSITIVE_KEYS and not public_chain:
                raise ValueError(f"Sensitive JSON key in {relative}: {'/'.join((*path, key))}")
            _scan_json_keys(child, relative, (*path, key))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _scan_json_keys(child, relative, (*path, str(index)))


def check_json(relative: str, content: bytes) -> None:
    if relative not in {f"data/{name}.json" for name in RUNTIME_NAMES}:
        raise ValueError(f"Not an allowed public JSON: {relative}")
    text = content.decode("utf-8", errors="strict")
    _scan_text(text, relative)
    value = json.loads(text)
    # Also scan decoded JSON: escaped field names must not evade the text boundary.
    _scan_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")), relative)
    _scan_json_keys(value, relative)


def check_tree(root: Path) -> None:
    root = root.resolve()
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if any(part in SKIP_DIRECTORIES for part in relative.parts):
            continue
        if path.is_symlink():
            raise ValueError(f"Symlink in public tree: {relative.as_posix()}")
        if not path.is_file():
            continue
        name = relative.as_posix()
        if not allowed_path(name):
            raise ValueError(f"File outside public allowlist: {name}")
        if name.startswith("data/") and name.endswith(".json"):
            check_json(name, path.read_bytes())
        else:
            _scan_text(path.read_text(encoding="utf-8", errors="strict"), name)


if __name__ == "__main__":
    check_tree(Path.cwd())
    print("Public repository boundary passed")
