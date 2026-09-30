"""Publish collector-owned files on the latest remote main without touching the runner checkout.

The collector writes into its original checkout for the whole run. Each publish uses an
isolated, temporary Git worktree at the freshly fetched remote main, so uncommitted
snapshots never enter a runtime-status commit and unrelated remote commits survive.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable

from scripts.public_repo_guard import check_json


STATUS_FILE = "data/system_status.json"
COLLECTOR_FILES = (
    "data/latest.json",
    "data/indicators.json",
    "data/market_summary.json",
    "data/intraday_summary.json",
    "data/optional_provider_test.json",
    "data/asset_identity_cache.json",
    "data/derivatives_summary.json",
    "data/derivatives_history.json",
    "data/identity_audit.json",
    STATUS_FILE,
)
MAX_ATTEMPTS = 3


class PublishError(RuntimeError):
    pass


class PublishConflict(PublishError):
    pass


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True)
    if check and result.returncode:
        raise PublishError(f"git {' '.join(args)} failed: {result.stderr.decode(errors='replace').strip()}")
    return result


def _blob(repo: Path, revision: str, path: str) -> bytes | None:
    if _git(repo, "cat-file", "-e", f"{revision}:{path}", check=False).returncode:
        return None
    return _git(repo, "show", f"{revision}:{path}").stdout


def _same_run_status(remote: bytes | None, local: bytes) -> bool:
    try:
        old = json.loads(remote or b"{}")
        new = json.loads(local)
        old_run = old["run"]
        new_run = new["run"]
        return (old_run.get("id") is not None and old_run["id"] == new_run.get("id")
                and datetime.fromisoformat(old["updated_at"].replace("Z", "+00:00"))
                <= datetime.fromisoformat(new["updated_at"].replace("Z", "+00:00")))
    except (KeyError, TypeError, ValueError):
        return False


class SafePublisher:
    def __init__(self, repo: Path, *, remote: str = "origin", branch: str = "main",
                 max_attempts: int = MAX_ATTEMPTS, backoff_seconds: float = 0.5,
                 before_push: Callable[[int], None] | None = None):
        self.repo = repo.resolve()
        self.remote = remote
        self.branch = branch
        self.max_attempts = max_attempts
        self.backoff_seconds = backoff_seconds
        self.before_push = before_push
        self.base_sha = _git(self.repo, "rev-parse", "HEAD").stdout.decode().strip()

    def publish(self, mode: str) -> str:
        if mode not in ("status", "final"):
            raise ValueError("mode must be status or final")
        files = (STATUS_FILE,) if mode == "status" else COLLECTOR_FILES
        last_push_error = ""
        for attempt in range(1, self.max_attempts + 1):
            _git(self.repo, "fetch", "--quiet", self.remote, self.branch)
            remote_sha = _git(self.repo, "rev-parse", "FETCH_HEAD").stdout.decode().strip()
            with tempfile.TemporaryDirectory(prefix="coincheck-publish-") as directory:
                root = Path(directory).resolve()
                checkout = root / "checkout"
                if not checkout.resolve().is_relative_to(root):
                    raise PublishError("Temporary checkout escaped its parent directory")
                _git(self.repo, "worktree", "add", "--detach", "--quiet", str(checkout), remote_sha)
                try:
                    staged: list[str] = []
                    for relative in files:
                        source = self.repo / relative
                        if not source.is_file():
                            continue
                        local = source.read_bytes()
                        check_json(relative, local)
                        base = _blob(self.repo, self.base_sha, relative)
                        remote = _blob(self.repo, remote_sha, relative)
                        if local == base or local == remote:
                            continue
                        if remote != base and not (relative == STATUS_FILE and
                                                   _same_run_status(remote, local)):
                            raise PublishConflict(f"Concurrent change to collector-owned {relative}; "
                                                  "refusing to overwrite remote main")
                        target = checkout / relative
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(local)
                        staged.append(relative)
                    if not staged:
                        print(f"No {mode} files changed against remote {remote_sha}")
                        return remote_sha
                    _git(checkout, "add", "--", *staged)
                    _git(checkout, "-c", "user.name=github-actions[bot]", "-c",
                         "user.email=41898282+github-actions[bot]@users.noreply.github.com",
                         "commit", "--quiet", "-m",
                         "data: publish collector status" if mode == "status"
                         else "data: update Upbit market snapshot")
                    if self.before_push:
                        self.before_push(attempt)
                    pushed = _git(checkout, "push", self.remote, f"HEAD:{self.branch}", check=False)
                    if pushed.returncode == 0:
                        sha = _git(checkout, "rev-parse", "HEAD").stdout.decode().strip()
                        print(f"Published {mode} commit {sha} on {remote_sha}; files: {', '.join(staged)}")
                        return sha
                    reason = pushed.stderr.decode(errors="replace").strip()
                    if not any(token in reason.lower() for token in
                               ("fetch first", "non-fast-forward")):
                        raise PublishError(f"Push failed without retryable main movement: {reason}")
                    last_push_error = reason
                    print(f"Remote main moved during {mode} publish attempt {attempt}/{self.max_attempts}; "
                          "fetching and replaying owned files", file=sys.stderr)
                finally:
                    _git(self.repo, "worktree", "remove", "--force", str(checkout))
            if attempt < self.max_attempts:
                time.sleep(self.backoff_seconds * attempt)
        raise PublishError(f"Remote main moved on all {self.max_attempts} publish attempts: "
                           f"{last_push_error}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("status", "final"))
    args = parser.parse_args()
    repo = Path(_git(Path.cwd(), "rev-parse", "--show-toplevel").stdout.decode().strip())
    publisher = SafePublisher(repo)
    try:
        published_sha = publisher.publish(args.mode)
        output = os.getenv("GITHUB_OUTPUT")
        if output:
            with Path(output).open("a", encoding="utf-8") as handle:
                handle.write(f"published_sha={published_sha}\n")
    except (PublishError, OSError, ValueError) as error:
        print(f"Collector {args.mode} publish failed: {error}", file=sys.stderr)
        if args.mode == "final":
            try:
                from collector.system_status import publish_failed
                publish_failed(repo / STATUS_FILE, str(error),
                               run_id=os.getenv("GITHUB_RUN_ID"))
                SafePublisher(repo).publish("status")
            except (PublishError, OSError, ValueError) as status_error:
                print(f"Could not publish failure status: {status_error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
