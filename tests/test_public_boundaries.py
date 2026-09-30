"""Fail closed if public output or source starts containing private material."""

import json
import hashlib
import tempfile
import unittest
from pathlib import Path

from collector.collection_targets import eligible_markets, rotating_markets
from scripts.public_repo_guard import _scan_text, allowed_path, check_json, check_tree
from scripts.validate_public_data import validate


class PublicBoundaryTests(unittest.TestCase):
    def test_current_tree_is_allowlisted(self):
        check_tree(Path(__file__).resolve().parents[1])

    def test_private_paths_rejected(self):
        for relative in ("prompts/coincheck.md", "data/coin_tracking.json",
                         "data/coincheck_reports.json", "data/app_settings.json",
                         "collector/app_data.py", "android/app/build.gradle.kts",
                         ".env", "local.properties", "secret.pem"):
            with self.subTest(relative=relative):
                self.assertFalse(allowed_path(relative))

    def test_sensitive_output_rejected(self):
        for key in ("token", "prompt", "recommendation", "tracking", "private_key"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                check_json("data/latest.json", json.dumps({key: "value"}).encode())
        with self.assertRaises(ValueError):
            check_json("data/coin_tracking.json", b"{}")

    def test_private_analysis_fields_rejected_in_json_and_collector_code(self):
        fields = ("project_score", "pump_setup_score", "trigger_score",
                  "proximity_score", "MFE", "MAE", "mfe_pct", "mae_pct",
                  "max_favorable_excursion", "max_adverse_excursion",
                  "partial_score", "coverage_score", "confidence_score",
                  "candidate_pool", "learning_loop", "project_360")
        for field in fields:
            with self.subTest(field=field):
                payload = {"markets": [{"market": "KRW-BTC", "details": {field: 1}}]}
                with self.assertRaises(ValueError):
                    check_json("data/latest.json", json.dumps(payload).encode())
                with self.assertRaises(ValueError):
                    _scan_text(f"{field} = 1", "collector/main.py")
        for field in ("projectScore", "pumpSetupScore", "triggerScore",
                      "proximityScore", "mfePct", "maePercent", "Project Score"):
            with self.subTest(field=field), self.assertRaises(ValueError):
                _scan_text(f"{field} = 1", "scripts/safe_publish.py")
        with self.assertRaises(ValueError):
            check_json("data/latest.json", b'{"project\\u005fscore":1}')

    def test_tree_rejects_private_strategy_in_allowed_runtime_module(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "collector").mkdir()
            (root / "collector/main.py").write_text("project_score = 1", encoding="utf-8")
            with self.assertRaises(ValueError):
                check_tree(root)

    def test_strategy_and_credential_text_are_rejected(self):
        for value in ("TOP1", "recommendation episode", "gh" + "p_" + "x" * 30,
                      "C:" + "\\Users\\" + "someone\\secret.txt"):
            with self.subTest(value=value[:12]), self.assertRaises(ValueError):
                _scan_text(value, "collector/unknown.py")

    def test_unexpected_file_and_symlink_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data").mkdir()
            (root / "data" / "coincheck_reports.json").write_text("{}", encoding="utf-8")
            with self.assertRaises(ValueError):
                check_tree(root)

    def test_neutral_rotation_excludes_invalid_observations(self):
        latest = {"markets": [
            {"market": "KRW-BTC", "ticker": {"trade_price": 100}},
            {"market": "KRW-ETH", "ticker": {"trade_price": 10}},
            {"market": "KRW-ARX", "ticker": {"trade_price": 0}},
            {"market": "USDT-SOL", "ticker": {"trade_price": 2}},
        ]}
        self.assertEqual(eligible_markets(latest), {"KRW-BTC", "KRW-ETH"})
        self.assertEqual(rotating_markets(latest, 1, 0), (["KRW-BTC"], 1))
        self.assertEqual(rotating_markets(latest, 1, 1), (["KRW-ETH"], 1))

    def test_public_data_accepts_empty_bootstrap_and_verified_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data").mkdir()
            self.assertEqual(validate(root), {})
            observed = "2026-09-29T00:01:00Z"
            latest = {"schema_version": "1.0", "collected_at": {"utc": observed},
                      "summary": {"market_count": 1, "successful_market_count": 1,
                                  "failed_market_count": 0}, "markets": []}
            raw = json.dumps(latest).encode()
            (root / "data/latest.json").write_bytes(raw)
            for name in ("indicators", "market_summary", "derivatives_summary"):
                key = "generated_at" if name == "market_summary" else "collected_at"
                (root / f"data/{name}.json").write_text(json.dumps({
                    "schema_version": "1.0", key: {"utc": observed}}), encoding="utf-8")
            status = {"schema_version": "1.0", "data_collected_at": observed,
                      "run": {"id": "123", "started_at": observed, "ended_at": observed,
                              "actions_result": "SUCCESS", "collection_result": "SUCCESS"},
                      "last_scheduled_success": {"collection_run_id": "123",
                          "data_collected_at": observed,
                          "market_snapshot_id": "sha256:" + hashlib.sha256(raw).hexdigest()}}
            (root / "data/system_status.json").write_text(json.dumps(status), encoding="utf-8")
            self.assertEqual(len(validate(root)), 5)
            status["last_scheduled_success"]["market_snapshot_id"] = "sha256:" + "0" * 64
            (root / "data/system_status.json").write_text(json.dumps(status), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "digest mismatch"):
                validate(root)

    def test_manual_snapshot_digest_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "data"
            data.mkdir()
            observed = "2026-09-29T00:01:00Z"
            latest = {"collected_at": {"utc": observed},
                      "summary": {"market_count": 1, "successful_market_count": 1,
                                  "failed_market_count": 0}}
            raw = json.dumps(latest).encode()
            (data / "latest.json").write_bytes(raw)
            for name in ("indicators", "market_summary", "derivatives_summary"):
                field = "generated_at" if name == "market_summary" else "collected_at"
                (data / f"{name}.json").write_text(json.dumps({field: {"utc": observed}}),
                                                   encoding="utf-8")
            status = {"data_collected_at": observed,
                      "run": {"id": "manual-123", "run_mode": "MANUAL",
                              "started_at": observed, "ended_at": observed,
                              "actions_result": "SUCCESS", "collection_result": "SUCCESS"},
                      "last_manual_success": {"collection_run_id": "manual-123",
                                              "data_collected_at": observed,
                                              "market_snapshot_id": "sha256:" +
                                              hashlib.sha256(raw).hexdigest()},
                      "last_scheduled_success": {"collection_run_id": "older-slot",
                                                 "market_snapshot_id": "sha256:" + "0" * 64}}
            status_path = data / "system_status.json"
            status_path.write_text(json.dumps(status), encoding="utf-8")
            self.assertEqual(len(validate(root)), 5)
            status["last_manual_success"]["market_snapshot_id"] = "sha256:" + "0" * 64
            status_path.write_text(json.dumps(status), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Manual run latest snapshot digest mismatch"):
                validate(root)


if __name__ == "__main__":
    unittest.main()
