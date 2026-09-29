"""Read-only collection health distinguishes absent runs from published success."""

import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from scripts.collection_health import assess, current_slot, parse_recovery_slot, workflow_runs
from scripts.collection_slot import slot_id


class CollectionHealthTest(unittest.TestCase):
    def setUp(self):
        self.slot = datetime(2026, 9, 27, 8, tzinfo=UTC)
        self.now = self.slot + timedelta(minutes=18)
        self.old_status = {"run": {"id": "old", "collection_slot":
                           slot_id(self.slot - timedelta(hours=4)),
                           "actions_result": "SUCCESS", "collection_result": "SUCCESS"}}

    def test_current_slot_and_exact_recovery_window(self):
        self.assertEqual(self.slot, current_slot(self.now))
        self.assertEqual(self.slot, parse_recovery_slot(slot_id(self.slot), self.now))
        for value in (slot_id(self.slot - timedelta(hours=4)),
                      slot_id(self.slot + timedelta(hours=4)),
                      "2026-09-27T08:05:00Z"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_recovery_slot(value, self.now)

    def test_previous_success_and_absent_run_are_not_current_success(self):
        self.assertEqual("NO_RUN_VISIBLE",
                         assess(self.slot, self.now, self.old_status, {}, []).state)
        self.assertEqual("WAITING", assess(self.slot, self.slot + timedelta(minutes=10),
                                           self.old_status, {}, []).state)

    def test_queued_or_running_run_blocks_missing_run_diagnosis(self):
        for state, expected in (("queued", "QUEUED"), ("pending", "QUEUED"),
                                ("in_progress", "RUNNING")):
            with self.subTest(state=state):
                self.assertEqual(expected, assess(self.slot, self.now, self.old_status, {},
                    [{"id": 25, "status": state}]).state)

    def test_failed_and_unverified_completion_remain_distinct(self):
        run = {"id": 25, "created_at": slot_id(self.slot + timedelta(minutes=1)),
               "status": "completed", "conclusion": "failure"}
        self.assertEqual("FAILED", assess(self.slot, self.now, self.old_status, {}, [run]).state)
        run["conclusion"] = "success"
        self.assertEqual("UNVERIFIED_COMPLETION",
                         assess(self.slot, self.now, self.old_status, {}, [run]).state)

    def test_actions_api_failure_is_not_treated_as_no_run(self):
        with patch("urllib.request.urlopen", side_effect=OSError("offline")):
            with self.assertRaisesRegex(OSError, "offline"):
                workflow_runs("owner/repo", "test-token")


if __name__ == "__main__":
    unittest.main()
