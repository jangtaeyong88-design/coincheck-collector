"""Runtime status transitions use actual provider observations, not app sync time."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from collector.system_status import (finish, provider_finish, provider_start,
                                     publish_failed, stage, start)
from scripts import collection_slot


class RuntimeStatusTest(unittest.TestCase):
    def setUp(self):
        # GitHub Actions sets GITHUB_RUN_ID for the test job. Local fixture run
        # IDs are independent of that job and must opt in to ownership checks.
        self.enterContext(patch.dict(os.environ, {"GITHUB_RUN_ID": ""}))
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "system_status.json"

    def snapshot(self, *, bitget=None, gate=None, kucoin=None):
        counts = {"bitget": bitget, "gate": gate, "kucoin": kucoin}
        counts = {name: row for name, row in counts.items() if row is not None}
        observed = int((datetime.now(UTC) - timedelta(seconds=10)).timestamp() * 1000)
        return {"collected_at": {"utc": datetime.now(UTC).isoformat()},
                "summary": {"market_count": 1, "provider_counts": counts}, "exchange_diagnostics": {},
                "markets": {"KRW-BTC": {name: {
                    "match": {"status": "VERIFIED"}, "oi": {"at_ms": observed}}
                    for name in counts}}}

    def test_recovery_trigger_preserves_scheduled_slot_and_old_schema(self):
        recovered = start(self.path, "recovery-1", "2026-09-27T08:00:00Z",
                          "SCHEDULED", "2026-09-27T08:18:00Z", "RECOVERY")
        self.assertEqual("RECOVERY", recovered["run"]["trigger"])
        self.assertEqual("SCHEDULED", recovered["run"]["run_mode"])
        self.assertEqual("RECOVERY", recovered["run"]["trigger"])
        with self.assertRaises(ValueError):
            start(self.path, "bad", run_mode="MANUAL", trigger="RECOVERY")

    def test_android_manual_trigger_does_not_claim_a_scheduled_slot(self):
        manual = start(self.path, "android-1", run_mode="MANUAL",
                       trigger="ANDROID_MANUAL")
        self.assertEqual("ANDROID_MANUAL", manual["run"]["trigger"])
        self.assertIsNone(manual["run"]["collection_slot"])
        self.assertIsNone(manual["run"]["collection_slot"])
        with self.assertRaises(ValueError):
            start(self.path, "bad", "2026-09-27T08:00:00Z", "SCHEDULED",
                  trigger="ANDROID_MANUAL")

    def test_run_and_provider_transitions_preserve_last_good_time(self):
        old = {"schema_version": "1.0", "run": {"collection_result": "SUCCESS"},
               "providers": {"bitget": {"status": "SUCCESS", "verified": 5,
                                           "last_success_at": "2026-09-25T01:00:00Z"}}}
        self.path.write_text(json.dumps(old), encoding="utf-8")
        running = start(self.path, "run-7", "2026-09-27T00:00:00Z")
        self.assertEqual("2026-09-27T00:00:00Z", running["run"]["collection_slot"])
        self.assertEqual(running["run"]["current_stage"], "UPBIT")
        self.assertEqual(running["providers"]["bitget"]["last_success_at"], "2026-09-25T01:00:00Z")
        stage(self.path, "DERIVATIVES")
        started = provider_start(self.path, "bitget")
        self.assertEqual(started["providers"]["bitget"]["status"], "RUNNING")
        self.assertIsNone(started["providers"]["bitget"]["ended_at"])
        snapshot = self.snapshot(bitget={"verified": 2, "missing_core_count": 0,
                                         "request_count": 9})
        ended = provider_finish(self.path, "bitget", snapshot)
        self.assertEqual("2026-09-27T00:00:00Z", ended["run"]["collection_slot"])
        self.assertEqual(ended["providers"]["bitget"]["status"], "SUCCESS")
        self.assertIsNotNone(ended["providers"]["bitget"]["ended_at"])
        # The stage result is published before the snapshot; last_success_at stays old.
        self.assertEqual(ended["providers"]["bitget"]["last_success_at"], "2026-09-25T01:00:00Z")

    def test_provider_partial_and_failure_are_independent(self):
        start(self.path, "run-8")
        for name in ("bitget", "gate", "kucoin"):
            provider_start(self.path, name)
            snapshot = self.snapshot(
                bitget={"verified": 3, "missing_core_count": 0},
                gate={"verified": 3, "missing_core_count": 1},
                kucoin={"verified": 0, "missing_core_count": 0})
            provider_finish(self.path, name, snapshot)
        status = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual([status["providers"][name]["status"]
                          for name in ("bitget", "gate", "kucoin")],
                         ["SUCCESS", "PARTIAL", "FAILED"])
        self.assertEqual(status["run"]["collection_result"], "RUNNING")

    def test_finish_records_partial_and_actual_provider_observation(self):
        start(self.path, "run-9")
        observed = self.snapshot(bitget={"verified": 1, "missing_core_count": 0},
                                 gate={"verified": 2, "missing_core_count": 1},
                                 kucoin={"verified": 1, "missing_core_count": 0})
        for name in ("latest", "indicators", "market_summary"):
            (self.root / f"{name}.json").write_text(json.dumps({
                ("generated_at" if name == "market_summary" else "collected_at"):
                    {"utc": datetime.now(UTC).isoformat()},
                "summary": {"market_count": 1, "successful_market_count": 1,
                            "failed_market_count": 0},
                "fields": ["price", "collection_success", "insufficient_data"] if
                          name == "market_summary" else [],
                "markets": {"KRW-BTC": [100, True, False]} if name == "market_summary" else
                           [{"market": "KRW-BTC"}]}), encoding="utf-8")
        (self.root / "derivatives_summary.json").write_text(json.dumps(observed), encoding="utf-8")
        result = finish(self.path, self.root, "SUCCESS")
        self.assertEqual(result["run"]["collection_result"], "PARTIAL_ACCEPTABLE")
        self.assertEqual(result["providers"]["gate"]["status"], "PARTIAL")
        self.assertIsNotNone(result["providers"]["bitget"]["last_success_at"])
        self.assertIsNotNone(result["providers"]["gate"]["last_success_at"])
        self.assertEqual(result["run"]["current_stage"], "FINALIZING")

    def test_failure_status_is_explicit(self):
        start(self.path, "run-10")
        result = publish_failed(self.path, "same-file conflict")
        self.assertEqual(result["run"]["collection_result"], "FAILED")
        self.assertEqual(result["run"]["current_stage"], "PUBLISH_FAILED")
        self.assertIsNotNone(result["run"]["ended_at"])
        self.assertIn("same-file conflict", result["errors"][-1])

    def test_publish_failure_restores_previous_success_times(self):
        previous = {"schema_version": "1.0", "run": {"collection_result": "SUCCESS"},
                    "last_success_at": "2026-09-25T00:00:00Z",
                    "providers": {"bitget": {"status": "SUCCESS", "verified": 1,
                                               "last_success_at": "2026-09-25T01:00:00Z"}}}
        self.path.write_text(json.dumps(previous), encoding="utf-8")
        start(self.path, "run-11")
        provider_start(self.path, "bitget")
        result = publish_failed(self.path, "retry limit")
        self.assertEqual(result["last_success_at"], "2026-09-25T00:00:00Z")
        self.assertEqual(result["providers"]["bitget"]["last_success_at"],
                         "2026-09-25T01:00:00Z")

    def test_failed_run_does_not_relabel_stale_derivatives_as_success(self):
        start(self.path, "run-12")
        old = self.snapshot(bitget={"verified": 1, "missing_core_count": 0})
        old["collected_at"] = {"utc": "2026-09-01T00:00:00Z"}
        (self.root / "derivatives_summary.json").write_text(json.dumps(old), encoding="utf-8")
        result = finish(self.path, self.root, "FAILURE")
        self.assertEqual(result["run"]["collection_result"], "FAILED")
        self.assertEqual(result["providers"]["bitget"]["status"], "FAILED")
        self.assertIsNone(result["providers"]["bitget"].get("last_success_at"))

    def test_all_fresh_sources_and_providers_finish_successfully(self):
        start(self.path, "run-13")
        observed = self.snapshot(bitget={"verified": 1, "missing_core_count": 0},
                                 gate={"verified": 1, "missing_core_count": 0},
                                 kucoin={"verified": 1, "missing_core_count": 0})
        for name in ("latest", "indicators", "market_summary"):
            field = "generated_at" if name == "market_summary" else "collected_at"
            (self.root / f"{name}.json").write_text(json.dumps({
                field: {"utc": datetime.now(UTC).isoformat()},
                "summary": {"market_count": 1, "successful_market_count": 1,
                            "failed_market_count": 0},
                "fields": ["price", "collection_success", "insufficient_data"] if
                          name == "market_summary" else [],
                "markets": {"KRW-BTC": [100, True, False]} if name == "market_summary" else
                           [{"market": "KRW-BTC"}]}), encoding="utf-8")
        (self.root / "derivatives_summary.json").write_text(json.dumps(observed), encoding="utf-8")
        result = finish(self.path, self.root, "SUCCESS")
        self.assertEqual(result["run"]["collection_result"], "SUCCESS")
        self.assertTrue(result["run"]["ended_at"])
        self.assertTrue(all(result["providers"][name]["status"] == "SUCCESS"
                            for name in ("bitget", "gate", "kucoin")))
        self.assertEqual("SCHEDULED", result["last_scheduled_success"]["run_mode"])
        self.assertEqual("run-13", result["last_successful_collection"]["collection_run_id"])

    def test_one_failed_market_is_acceptable_but_six_hold_the_collection(self):
        for failed in (1, 6):
            with self.subTest(failed=failed):
                start(self.path, f"partial-{failed}")
                codes = [f"KRW-T{i:03d}" for i in range(290)]
                summary = {"market_count": 290, "successful_market_count": 290 - failed,
                           "failed_market_count": failed}
                observed = datetime.now(UTC).isoformat()
                latest = {"collected_at": {"utc": observed}, "summary": summary,
                          "markets": [{"market": code} for code in codes]}
                indicators = {"collected_at": {"utc": observed}, "summary": summary,
                              "markets": [{"market": code} for code in codes]}
                market = {"generated_at": {"utc": observed}, "summary": summary,
                          "fields": ["price", "collection_success", "insufficient_data"],
                          "markets": {code: [100, index < 290 - failed, False]
                                      for index, code in enumerate(codes)}}
                derivatives = self.snapshot(bitget={"verified": 1}, gate={"verified": 1},
                                            kucoin={"verified": 1})
                derivatives["summary"]["market_count"] = 290
                derivatives["markets"] = {code: {} for code in codes}
                for name, document in (("latest", latest), ("indicators", indicators),
                                       ("market_summary", market),
                                       ("derivatives_summary", derivatives)):
                    (self.root / f"{name}.json").write_text(json.dumps(document), encoding="utf-8")
                result = finish(self.path, self.root, "SUCCESS")
                self.assertEqual("PARTIAL_ACCEPTABLE" if failed == 1 else "FAILED",
                                 result["run"]["collection_result"])

    def test_manual_success_preserves_scheduled_history(self):
        scheduled = {"collection_run_id": "scheduled-1", "run_mode": "SCHEDULED",
                     "collection_slot": "2026-09-27T00:00:00Z",
                     "collection_result": "SUCCESS", "actions_result": "SUCCESS"}
        self.path.write_text(json.dumps({"schema_version": "1.0",
            "last_scheduled_run": scheduled, "last_scheduled_success": scheduled,
            "last_successful_collection": scheduled, "providers": {}}), encoding="utf-8")
        running = start(self.path, "manual-2", run_mode="MANUAL")
        self.assertIsNone(running["run"]["collection_slot"])
        self.assertEqual(scheduled, running["last_scheduled_success"])
        observed = self.snapshot(bitget={"verified": 1, "missing_core_count": 0},
                                 gate={"verified": 1, "missing_core_count": 0},
                                 kucoin={"verified": 1, "missing_core_count": 0})
        for name in ("latest", "indicators", "market_summary"):
            field = "generated_at" if name == "market_summary" else "collected_at"
            (self.root / f"{name}.json").write_text(json.dumps({
                field: {"utc": datetime.now(UTC).isoformat()},
                "summary": {"market_count": 1, "successful_market_count": 1,
                            "failed_market_count": 0},
                "fields": ["price", "collection_success", "insufficient_data"] if
                          name == "market_summary" else [],
                "markets": {"KRW-BTC": [100, True, False]} if name == "market_summary" else
                           [{"market": "KRW-BTC"}]}), encoding="utf-8")
        (self.root / "derivatives_summary.json").write_text(json.dumps(observed), encoding="utf-8")
        finished = finish(self.path, self.root, "SUCCESS")
        self.assertEqual("SUCCESS", finished["run"]["collection_result"])
        self.assertEqual("MANUAL", finished["last_successful_collection"]["run_mode"])
        self.assertEqual("manual-2", finished["last_manual_success"]["collection_run_id"])
        self.assertEqual(scheduled, finished["last_scheduled_success"])

    def test_final_publish_failure_cannot_leave_unpublished_success(self):
        previous = {"schema_version": "1.0", "providers": {},
                    "last_success_at": "2026-09-26T00:30:00Z",
                    "data_collected_at": "2026-09-26T00:02:00Z",
                    "last_scheduled_success": {"collection_run_id": "old", "run_mode": "SCHEDULED",
                                               "collection_result": "SUCCESS"}}
        self.path.write_text(json.dumps(previous), encoding="utf-8")
        start(self.path, "new-run", "2026-09-27T00:00:00Z")
        observed = self.snapshot(bitget={"verified": 1, "missing_core_count": 0},
                                 gate={"verified": 1, "missing_core_count": 0},
                                 kucoin={"verified": 1, "missing_core_count": 0})
        for name in ("latest", "indicators", "market_summary"):
            field = "generated_at" if name == "market_summary" else "collected_at"
            (self.root / f"{name}.json").write_text(json.dumps({
                field: {"utc": datetime.now(UTC).isoformat()},
                "summary": {"market_count": 1, "successful_market_count": 1,
                            "failed_market_count": 0},
                "fields": ["price", "collection_success", "insufficient_data"] if
                          name == "market_summary" else [],
                "markets": {"KRW-BTC": [100, True, False]} if name == "market_summary" else
                           [{"market": "KRW-BTC"}]}), encoding="utf-8")
        (self.root / "derivatives_summary.json").write_text(json.dumps(observed), encoding="utf-8")
        self.assertEqual("SUCCESS", finish(self.path, self.root, "SUCCESS")["run"]["collection_result"])
        failed = publish_failed(self.path, "push rejected")
        self.assertEqual("FAILED", failed["run"]["collection_result"])
        self.assertEqual("old", failed["last_scheduled_success"]["collection_run_id"])
        self.assertEqual("2026-09-26T00:30:00Z", failed["last_success_at"])
        self.assertEqual("2026-09-26T00:02:00Z", failed["data_collected_at"])

    def test_all_cli_triggers_match_start_contract(self):
        cases = (("SCHEDULE", "SCHEDULED", "2026-09-29T00:00:00Z"),
                 ("RECOVERY", "SCHEDULED", "2026-09-29T00:00:00Z"),
                 ("CLOUDFLARE_SCHEDULE", "SCHEDULED", "2026-09-29T00:00:00Z"),
                 ("CLOUDFLARE_RETRY", "SCHEDULED", "2026-09-29T00:00:00Z"),
                 ("MANUAL", "MANUAL", None), ("ANDROID_MANUAL", "MANUAL", None))
        for index, (trigger, mode, slot) in enumerate(cases):
            with self.subTest(trigger=trigger):
                path = self.root / f"cli-{index}.json"
                args = [sys.executable, "-m", "collector.system_status", "start",
                        "--output", str(path), "--run-mode", mode, "--trigger", trigger]
                if slot:
                    args += ["--slot", slot]
                result = subprocess.run(args, capture_output=True, text=True, check=False,
                                        env={**os.environ, "GITHUB_RUN_ID": f"cli-{index}"})
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertEqual(trigger, json.loads(path.read_text(encoding="utf-8"))["run"]["trigger"])

    def test_cloudflare_slot_output_can_start_status_unchanged(self):
        for source, expected in (("cloudflare_schedule", "CLOUDFLARE_SCHEDULE"),
                                 ("cloudflare_retry", "CLOUDFLARE_RETRY")):
            with self.subTest(source=source):
                output = self.root / f"{source}.output"
                with patch.dict(os.environ, {
                    "GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_OUTPUT": str(output),
                    "COLLECTION_RECOVERY_SLOT": "2026-09-29T00:00:00Z",
                    "COLLECTION_SCHEDULED_SOURCE": source, "GITHUB_RUN_ID": source,
                    "GITHUB_REPOSITORY": "owner/repo", "GITHUB_TOKEN": "test-token",
                }, clear=True), patch("scripts.collection_slot.datetime", wraps=datetime) as clock, \
                    patch("scripts.collection_slot._git", side_effect=[b"", b"main-sha"]), \
                    patch("scripts.collection_slot._remote_json", return_value={}), \
                    patch("scripts.collection_health.workflow_runs", return_value=[]):
                    clock.now.return_value = datetime(2026, 9, 29, 0, 1, tzinfo=UTC)
                    self.assertEqual(0, collection_slot.main())
                outputs = dict(line.split("=", 1) for line in output.read_text().splitlines())
                self.assertEqual("true", outputs["collect"])
                self.assertEqual(expected, outputs["trigger"])
                path = self.root / f"{source}.json"
                args = [sys.executable, "-m", "collector.system_status", "start",
                        "--output", str(path), "--run-mode", outputs["run_mode"],
                        "--trigger", outputs["trigger"], "--slot", outputs["slot"],
                        "--requested-at", outputs["requested_at"]]
                result = subprocess.run(args, capture_output=True, text=True, check=False,
                                        env={**os.environ, "GITHUB_RUN_ID": source})
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertEqual(expected, json.loads(path.read_text())["run"]["trigger"])

    def test_pre_start_failure_isolated_from_previous_success(self):
        previous = start(self.path, "old", "2026-09-28T12:00:00Z", trigger="RECOVERY")
        previous["run"].update(actions_result="SUCCESS", collection_result="SUCCESS",
                               ended_at="2026-09-28T12:33:36Z", current_stage="FINALIZING")
        summary = {"collection_run_id": "old", "run_mode": "SCHEDULED",
                   "collection_slot": "2026-09-28T12:00:00Z", "actions_result": "SUCCESS",
                   "collection_result": "SUCCESS"}
        previous.update(last_success_at="2026-09-28T12:33:36Z",
                        data_collected_at="2026-09-28T12:02:01Z",
                        last_scheduled_success=summary, last_successful_collection=summary)
        self.path.write_text(json.dumps(previous), encoding="utf-8")
        with patch.dict(os.environ, {"GITHUB_RUN_ID": "new"}):
            with self.assertRaises(ValueError):
                publish_failed(self.path, "missing provenance", run_id=os.getenv("GITHUB_RUN_ID"))
            self.assertEqual(previous, json.loads(self.path.read_text(encoding="utf-8")))
            with self.assertRaises(ValueError):
                finish(self.path, self.root, "FAILURE", run_id=os.getenv("GITHUB_RUN_ID"))
            result = publish_failed(self.path, "start rejected Cloudflare trigger",
                                    run_id=os.getenv("GITHUB_RUN_ID"),
                                    run_mode="SCHEDULED", slot="2026-09-28T16:00:00Z",
                                    trigger="CLOUDFLARE_RETRY")
        self.assertEqual("new", result["run"]["id"])
        self.assertEqual("FAILED", result["run"]["collection_result"])
        self.assertEqual("CLOUDFLARE_RETRY", result["run"]["trigger"])
        self.assertEqual("2026-09-28T16:00:00Z", result["run"]["collection_slot"])
        self.assertEqual(summary, result["last_scheduled_success"])
        self.assertEqual(summary, result["last_successful_collection"])
        self.assertEqual("2026-09-28T12:33:36Z", result["last_success_at"])
        self.assertEqual("2026-09-28T12:02:01Z", result["data_collected_at"])

    def test_pre_start_failure_without_existing_status_uses_current_run(self):
        with patch.dict(os.environ, {"GITHUB_RUN_ID": "first-run"}):
            result = publish_failed(self.path, "start failed", run_mode="MANUAL",
                                    trigger="ANDROID_MANUAL", run_id=os.getenv("GITHUB_RUN_ID"))
        self.assertEqual("first-run", result["run"]["collection_run_id"])
        self.assertEqual("MANUAL", result["run"]["run_mode"])
        self.assertIsNone(result["run"]["collection_slot"])
        self.assertEqual("FAILED", result["run"]["collection_result"])
        self.assertEqual("FAILED", result["run"]["collection_result"])

    def test_workflow_failure_handler_passes_slot_outputs_and_skips_unstarted_finish(self):
        workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/collect.yml").read_text()
        self.assertIn("id: start_status", workflow)
        self.assertIn("steps.start_status.outcome == 'success'", workflow)
        for name in ("run_mode", "slot", "trigger", "requested_at"):
            self.assertIn(f"steps.slot.outputs.{name}", workflow)
        self.assertIn('publish-failed \\', workflow)
        self.assertIn('"${args[@]}"', workflow)

    def test_same_run_and_manual_failure_keep_provenance(self):
        for trigger, mode, slot in (("CLOUDFLARE_SCHEDULE", "SCHEDULED", "2026-09-29T00:00:00Z"),
                                    ("RECOVERY", "SCHEDULED", "2026-09-29T00:00:00Z"),
                                    ("MANUAL", "MANUAL", None),
                                    ("ANDROID_MANUAL", "MANUAL", None)):
            with self.subTest(trigger=trigger), patch.dict(os.environ, {"GITHUB_RUN_ID": trigger}):
                start(self.path, trigger, slot, mode, trigger=trigger)
                result = publish_failed(self.path, "same run failure",
                                        run_id=os.getenv("GITHUB_RUN_ID"))
                self.assertEqual(trigger, result["run"]["trigger"])
                self.assertEqual(mode, result["run"]["run_mode"])
                self.assertEqual(slot, result["run"]["collection_slot"])
                self.assertEqual("PUBLISH_FAILED", result["run"]["current_stage"])
                self.assertEqual(trigger, result["run"]["trigger"])


if __name__ == "__main__":
    unittest.main()
