"""Scheduled retries are idempotent and use actual published collection evidence."""

import copy
import unittest
from datetime import UTC, datetime, timedelta

from scripts.collection_slot import (cloudflare_window_open, decide, slot_for_event,
                                     slot_id, slot_succeeded)


class CollectionSlotTest(unittest.TestCase):
    def setUp(self):
        self.slot = datetime(2026, 9, 27, 0, tzinfo=UTC)
        started = self.slot + timedelta(minutes=1)
        ended = self.slot + timedelta(minutes=28)
        self.status = {
            "run": {"id": "123", "collection_slot": slot_id(self.slot),
                    "started_at": slot_id(started), "ended_at": slot_id(ended),
                    "actions_result": "SUCCESS", "collection_result": "SUCCESS"},
            "last_success_at": slot_id(ended),
            "data_collected_at": slot_id(started + timedelta(minutes=1)),
        }
        self.documents = {
            "latest": {"collected_at": {"utc": slot_id(started + timedelta(minutes=1))},
                       "summary": {"market_count": 289, "successful_market_count": 289,
                                   "failed_market_count": 0}},
            "indicators": {"collected_at": {"utc": slot_id(started + timedelta(minutes=5))}},
            "market_summary": {"generated_at": {"utc": slot_id(started + timedelta(minutes=8))}},
            "derivatives_summary": {"collected_at": {"utc": slot_id(started + timedelta(minutes=20))}},
        }

    def test_all_kst_slots_and_retry_minutes_map_to_same_slot(self):
        for hour in (0, 4, 8, 12, 16, 20):
            for minute in (0, 5, 10, 15):
                now = self.slot.replace(hour=hour, minute=minute)
                cron = f"0,5,10,15 {hour} * * *"
                with self.subTest(hour=hour, minute=minute):
                    self.assertEqual(now.replace(minute=0), slot_for_event("schedule", cron, now))
        self.assertIsNone(slot_for_event("schedule", "0,5,10,15 0 * * *",
                                         self.slot + timedelta(hours=4)))

    def test_cloudflare_dispatch_cannot_start_early_or_after_window(self):
        self.assertFalse(cloudflare_window_open(self.slot, self.slot - timedelta(seconds=1)))
        self.assertTrue(cloudflare_window_open(self.slot, self.slot))
        self.assertTrue(cloudflare_window_open(self.slot, self.slot + timedelta(minutes=30)))
        self.assertFalse(cloudflare_window_open(self.slot, self.slot + timedelta(minutes=30, seconds=1)))

    def test_completed_slot_skips_all_retries(self):
        self.assertTrue(slot_succeeded(self.status, self.documents, self.slot))
        for minute in (5, 10, 15):
            with self.subTest(minute=minute):
                selected = slot_for_event("schedule", "0,5,10,15 0 * * *",
                                          self.slot + timedelta(minutes=minute))
                self.assertEqual("SUCCESS_ALREADY_COLLECTED",
                                 decide(selected, self.status, self.documents).reason)

    def test_failure_at_each_attempt_collects_at_next_attempt(self):
        failed = copy.deepcopy(self.status)
        failed["run"]["collection_result"] = "FAILED"
        for minute in (5, 10, 15):
            with self.subTest(minute=minute):
                selected = slot_for_event("schedule", "0,5,10,15 0 * * *",
                                          self.slot + timedelta(minutes=minute))
                self.assertTrue(decide(selected, failed, self.documents).collect)
        self.assertTrue(decide(self.slot, None, {}).collect)

    def test_successful_five_minute_retry_skips_ten_and_fifteen(self):
        retried = copy.deepcopy(self.status)
        retried["run"]["started_at"] = slot_id(self.slot + timedelta(minutes=5))
        observed = copy.deepcopy(self.documents)
        for name, document in observed.items():
            field = "generated_at" if name == "market_summary" else "collected_at"
            document[field]["utc"] = slot_id(self.slot + timedelta(minutes=6))
        retried["data_collected_at"] = observed["latest"]["collected_at"]["utc"]
        for minute in (10, 15):
            self.assertEqual("SUCCESS_ALREADY_COLLECTED",
                             decide(self.slot, retried, observed).reason)

    def test_active_run_blocks_duplicate_but_stale_running_can_retry(self):
        running = copy.deepcopy(self.status)
        running["run"].update(collection_result="RUNNING", ended_at=None)
        self.assertEqual("SKIPPED_RUNNING",
                         decide(self.slot, running, {}, previous_run_active=True).reason)
        self.assertTrue(decide(self.slot, running, {}, previous_run_active=False).collect)

    def test_manual_request_can_recollect_completed_slot_but_not_overlap(self):
        self.assertIsNone(slot_for_event("workflow_dispatch", None,
                                         self.slot + timedelta(hours=2)))
        self.assertTrue(decide(self.slot, self.status, self.documents, force_manual=True).collect)
        running = copy.deepcopy(self.status)
        running["run"]["collection_result"] = "RUNNING"
        self.assertFalse(decide(self.slot, running, {}, previous_run_active=True,
                                force_manual=True).collect)

    def test_manual_latest_does_not_erase_scheduled_retry_success(self):
        manual = copy.deepcopy(self.status)
        manual["run"].update(run_mode="MANUAL", collection_slot=None)
        manual["last_scheduled_success"] = {
            "collection_run_id": "123", "run_mode": "SCHEDULED",
            "collection_slot": slot_id(self.slot),
            "started_at": self.status["run"]["started_at"],
            "ended_at": self.status["run"]["ended_at"],
            "actions_result": "SUCCESS", "collection_result": "SUCCESS",
            "data_collected_at": self.status["data_collected_at"],
            "market_snapshot_id": "sha256:" + "a" * 64,
            "upbit": {"market_count": 289, "success_count": 289, "failure_count": 0},
            "source_collected_at": {
                name: document["generated_at" if name == "market_summary" else "collected_at"]["utc"]
                for name, document in self.documents.items()},
        }
        self.assertEqual("SUCCESS_ALREADY_COLLECTED", decide(self.slot, manual, {}).reason)
        self.assertTrue(decide(self.slot + timedelta(hours=4), manual, {}).collect)
        manual["last_scheduled_success"]["source_collected_at"]["indicators"] = \
            slot_id(self.slot - timedelta(hours=1))
        self.assertTrue(decide(self.slot, manual, {}).collect)

    def test_prior_slot_success_cannot_skip_new_slot(self):
        self.assertTrue(decide(self.slot + timedelta(hours=4), self.status,
                               self.documents).collect)

    def test_recorded_success_survives_later_failed_attempt_in_same_slot(self):
        status = copy.deepcopy(self.status)
        status["last_scheduled_success"] = {
            "run_mode": "SCHEDULED", "collection_slot": slot_id(self.slot),
            "actions_result": "SUCCESS", "collection_result": "SUCCESS",
            "started_at": self.status["run"]["started_at"],
            "ended_at": self.status["run"]["ended_at"],
            "data_collected_at": self.status["data_collected_at"],
            "market_snapshot_id": "sha256:" + "a" * 64,
            "upbit": {"market_count": 289, "success_count": 289, "failure_count": 0},
            "source_collected_at": {
                name: document["generated_at" if name == "market_summary" else "collected_at"]["utc"]
                for name, document in self.documents.items()},
        }
        status["run"].update(actions_result="FAILURE", collection_result="FAILED")
        self.assertFalse(decide(self.slot, status, {}).collect)

    def test_manual_success_does_not_hide_failed_scheduled_slot(self):
        manual = copy.deepcopy(self.status)
        manual["run"].update(run_mode="MANUAL", collection_slot=None)
        manual["last_scheduled_run"] = {
            "run_mode": "SCHEDULED", "collection_slot": slot_id(self.slot),
            "collection_result": "FAILED"}
        manual["last_scheduled_success"] = {
            "run_mode": "SCHEDULED",
            "collection_slot": slot_id(self.slot - timedelta(hours=4)),
            "collection_result": "SUCCESS"}
        self.assertTrue(decide(self.slot, manual, {}).collect)

    def test_missing_or_stale_source_or_partial_result_cannot_skip(self):
        for name in self.documents:
            incomplete = copy.deepcopy(self.documents)
            del incomplete[name]
            with self.subTest(name=name):
                self.assertTrue(decide(self.slot, self.status, incomplete).collect)
        stale = copy.deepcopy(self.documents)
        stale["indicators"]["collected_at"]["utc"] = slot_id(self.slot - timedelta(hours=4))
        self.assertTrue(decide(self.slot, self.status, stale).collect)
        partial = copy.deepcopy(self.status)
        partial["run"]["collection_result"] = "PARTIAL"
        self.assertTrue(decide(self.slot, partial, self.documents).collect)

    def test_legacy_status_without_slot_uses_run_start_only_for_same_slot(self):
        legacy = copy.deepcopy(self.status)
        del legacy["run"]["collection_slot"]
        self.assertTrue(slot_succeeded(legacy, self.documents, self.slot))
        self.assertFalse(slot_succeeded(legacy, self.documents, self.slot + timedelta(hours=4)))


if __name__ == "__main__":
    unittest.main()
