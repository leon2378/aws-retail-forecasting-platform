"""Exercise local operations commands and their input/output boundaries."""
import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from retail_forecast.cli import main
from retail_forecast.storage import LocalRepository


def snapshot():
    return {"source": "synthetic", "source_label": "Generated operations fixture", "start_date": "2020-01-01",
            "total_days": 224, "metadata": {}, "series": [
                {"store_id": "CA_1", "item_id": "FOODS_1_001", "name": "Fixture item", "category": "FOODS",
                 "department": "FOODS_1", "price": 4,
                 "values": [4, 6, 8, 10, 12, 14, 16] * 32}]}


class OperationsCommandTests(unittest.TestCase):
    def test_rejected_snapshot_writes_evidence_and_exits_unsuccessfully(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = root / "candidate.json"
            original = '{"source":"synthetic","total_days":224,"series":[]}'
            candidate.write_text(original, encoding="utf-8")
            report_path = root / "quality.json"
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as stopped:
                main(["validate-snapshot", str(candidate), "--output", str(report_path)])
            self.assertEqual(stopped.exception.code, 1)
            report = json.loads(report_path.read_text())
            self.assertFalse(report["passed"])
            self.assertTrue(report["issues"])
            self.assertEqual(candidate.read_text(), original)

    def test_expected_contract_blocks_missing_product_without_changing_input(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            expected = snapshot()
            extra = copy.deepcopy(expected["series"][0])
            extra["item_id"] = "FOODS_1_002"
            expected["series"].append(extra)
            expected_path, candidate_path, report_path = root / "expected.json", root / "candidate.json", root / "report.json"
            expected_path.write_text(json.dumps(expected))
            candidate_path.write_text(json.dumps(snapshot()))
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as stopped:
                main(["validate-snapshot", str(candidate_path), "--expected", str(expected_path), "--output", str(report_path)])
            self.assertEqual(stopped.exception.code, 1)
            self.assertFalse(json.loads(report_path.read_text())["passed"])
            self.assertEqual(len(json.loads(candidate_path.read_text())["series"]), 1)

    def test_drill_export_persists_and_does_not_replace_working_dataset(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "dataset.json"
            source.write_text(json.dumps(snapshot()))
            before = source.read_bytes()
            report_path, status_path = root / "drill.json", root / "status.json"
            with contextlib.redirect_stdout(io.StringIO()):
                main(["recovery-drill", "--data-dir", str(root), "--output", str(report_path)])
                main(["operations", "--data-dir", str(root), "--output", str(status_path)])
            report, status = json.loads(report_path.read_text()), json.loads(status_path.read_text())
            self.assertEqual(report["latest_drill"]["status"], "completed")
            for key in ("invalid_snapshot_blocked", "interrupted_batch_hidden", "retry_idempotent", "rollback_restored"):
                self.assertTrue(report["latest_drill"]["proof"][key])
            self.assertEqual(status["latest_drill"]["id"], report["latest_drill"]["id"])
            self.assertEqual(source.read_bytes(), before)

    def test_explicit_import_approves_new_contract_and_keeps_audit_history(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "dataset.json"
            source.write_text(json.dumps(snapshot()))
            before = LocalRepository(root).operations()
            imported = snapshot()
            # Generated schema fixture exercises explicit importer approval, not authentic M5 sales.
            imported["source"] = "m5"
            imported["source_label"] = "Generated importer schema fixture"
            imported["series"][0]["item_id"] = "HOBBIES_1_001"
            with patch("retail_forecast.data.import_m5", return_value=imported), contextlib.redirect_stdout(io.StringIO()):
                main(["import-m5", "unused-fixture-folder", "--output", str(source)])
            after = LocalRepository(root).operations()
            self.assertEqual(after["source"], "m5")
            self.assertNotEqual(after["summary"]["active_version"], before["summary"]["active_version"])
            self.assertGreater(len(after["events"]), len(before["events"]))

    def test_invalid_explicit_import_preserves_source_and_active_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "dataset.json"
            source.write_text(json.dumps(snapshot()))
            original = source.read_bytes()
            before = LocalRepository(root).operations()["summary"]["active_version"]
            imported = snapshot()
            imported["series"][0]["values"][0] = -1
            with patch("retail_forecast.data.import_m5", return_value=imported), \
                    contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as stopped:
                main(["import-m5", "unused-fixture-folder", "--output", str(source)])
            self.assertEqual(stopped.exception.code, 2)
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(LocalRepository(root).operations()["summary"]["active_version"], before)


if __name__ == "__main__":
    unittest.main()
