"""A local bare repository exercises public collector publish races offline."""

import json
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from scripts.safe_publish import PublishConflict, PublishError, SafePublisher


def git(cwd, *args):
    result = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    if result.returncode:
        raise AssertionError(f"git {' '.join(args)}: {result.stderr}")
    return result.stdout.strip()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


class SafePublishTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.remote = self.root / "remote.git"
        self.seed = self.root / "seed"
        self.runner = self.root / "runner"
        self.writer = self.root / "writer"
        git(self.root, "init", "--bare", str(self.remote))
        git(self.root, "init", "-b", "main", str(self.seed))
        git(self.seed, "config", "user.email", "tests@example.com")
        git(self.seed, "config", "user.name", "Tests")
        git(self.seed, "remote", "add", "origin", str(self.remote))
        at = (datetime.now(UTC) - timedelta(minutes=15)).isoformat().replace("+00:00", "Z")
        write_json(self.seed / "data/latest.json", {"schema_version": "1.0",
            "collected_at": {"utc": at}, "markets": []})
        write_json(self.seed / "data/system_status.json", {"schema_version": "1.0",
            "run": {"id": "old", "collection_result": "SUCCESS"}, "updated_at": at})
        (self.seed / "README.md").write_text("original\n", encoding="utf-8")
        git(self.seed, "add", ".")
        git(self.seed, "commit", "-m", "seed")
        git(self.seed, "push", "-u", "origin", "main")
        git(self.remote, "symbolic-ref", "HEAD", "refs/heads/main")
        git(self.root, "clone", str(self.remote), str(self.runner))
        git(self.root, "clone", str(self.remote), str(self.writer))
        git(self.writer, "config", "user.email", "writer@example.com")
        git(self.writer, "config", "user.name", "Writer")

    def collector_status(self):
        now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        write_json(self.runner / "data/system_status.json", {"schema_version": "1.0",
            "run": {"id": "run-7", "collection_result": "RUNNING"}, "updated_at": now})

    def writer_commit(self, path, content):
        git(self.writer, "pull", "--ff-only", "origin", "main")
        target = self.writer / path
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, dict):
            write_json(target, content)
        else:
            target.write_text(content, encoding="utf-8")
        git(self.writer, "add", "--", path)
        git(self.writer, "commit", "-m", "concurrent change")
        git(self.writer, "push", "origin", "main")

    def remote_json(self, path):
        git(self.writer, "pull", "--ff-only", "origin", "main")
        return json.loads((self.writer / path).read_text(encoding="utf-8"))

    def test_status_commit_does_not_stage_unfinished_market_data(self):
        self.collector_status()
        write_json(self.runner / "data/latest.json", {"unfinished": True})
        SafePublisher(self.runner, backoff_seconds=0).publish("status")
        self.assertNotIn("unfinished", self.remote_json("data/latest.json"))
        self.assertEqual(self.remote_json("data/system_status.json")["run"]["id"], "run-7")

    def test_first_run_can_publish_from_main_with_no_existing_json(self):
        git(self.seed, "rm", "data/latest.json", "data/system_status.json")
        git(self.seed, "commit", "-m", "empty public data")
        git(self.seed, "push", "origin", "main")
        git(self.runner, "pull", "--ff-only", "origin", "main")
        git(self.writer, "pull", "--ff-only", "origin", "main")
        self.collector_status()
        SafePublisher(self.runner, backoff_seconds=0).publish("status")
        self.assertEqual(self.remote_json("data/system_status.json")["run"]["id"], "run-7")
        observed = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        write_json(self.runner / "data/latest.json", {"schema_version": "1.0",
            "collected_at": {"utc": observed}, "markets": []})
        write_json(self.runner / "data/system_status.json", {"schema_version": "1.0",
            "run": {"id": "run-7", "collection_result": "SUCCESS"},
            "updated_at": (datetime.now(UTC) + timedelta(seconds=1)).isoformat()})
        SafePublisher(self.runner, backoff_seconds=0).publish("final")
        self.assertEqual(self.remote_json("data/latest.json")["collected_at"]["utc"], observed)

    def test_final_publishes_only_collector_owned_files(self):
        self.collector_status()
        write_json(self.runner / "data/latest.json", {"schema_version": "1.0",
            "collected_at": {"utc": datetime.now(UTC).isoformat()}, "markets": []})
        write_json(self.runner / "data/not_allowlisted.json", {"unrelated": True})
        SafePublisher(self.runner, backoff_seconds=0).publish("final")
        self.assertTrue(self.remote_json("data/latest.json")["markets"] == [])
        self.assertNotIn("data/not_allowlisted.json", git(self.writer, "ls-files"))
        self.assertNotIn("data/not_allowlisted.json", git(self.runner, "ls-files"))

    def test_remote_code_commit_during_push_is_preserved_by_retry(self):
        self.collector_status()
        def race(attempt):
            if attempt == 1:
                self.writer_commit("README.md", "new remote code\n")
        SafePublisher(self.runner, backoff_seconds=0, before_push=race).publish("status")
        git(self.writer, "pull", "--ff-only", "origin", "main")
        self.assertEqual((self.writer / "README.md").read_text(), "new remote code\n")
        self.assertEqual(self.remote_json("data/system_status.json")["run"]["id"], "run-7")

    def test_other_run_status_change_is_not_overwritten(self):
        self.collector_status()
        self.writer_commit("data/system_status.json", {"schema_version": "1.0",
            "run": {"id": "other-run"}, "updated_at": datetime.now(UTC).isoformat()})
        with self.assertRaises(PublishConflict):
            SafePublisher(self.runner, backoff_seconds=0).publish("status")
        self.assertEqual(self.remote_json("data/system_status.json")["run"]["id"], "other-run")

    def test_retry_limit_stops_after_three_remote_moves(self):
        self.collector_status()
        attempts = []
        def race(attempt):
            attempts.append(attempt)
            self.writer_commit("README.md", f"remote revision {attempt}\n")
        with self.assertRaises(PublishError):
            SafePublisher(self.runner, max_attempts=3, backoff_seconds=0,
                          before_push=race).publish("status")
        self.assertEqual(attempts, [1, 2, 3])
        self.assertEqual(self.remote_json("data/system_status.json")["run"]["id"], "old")


if __name__ == "__main__":
    unittest.main()
