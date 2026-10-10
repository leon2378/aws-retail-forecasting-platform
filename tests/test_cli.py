"""Administration failures must leave working records intact and return useful errors."""
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from orderflow.cli import main
from orderflow.engine import Engine, validate_state
from orderflow.storage import SQLiteRepository


class AdministrationTests(unittest.TestCase):
    def setUp(self):
        scratch = Path(__file__).resolve().parents[1] / "build"
        scratch.mkdir(exist_ok=True)
        self.workspace = TemporaryDirectory(dir=scratch)
        self.database = Path(self.workspace.name) / "orders.db"
        self.repository = SQLiteRepository(self.database)
        self.engine = Engine(self.repository)

    def tearDown(self):
        self.workspace.cleanup()

    def invoke(self, *arguments):
        output = StringIO()
        with redirect_stdout(output):
            status = main([*arguments, "--database", str(self.database)])
        return status, json.loads(output.getvalue())

    def test_invalid_processing_limit_is_structured_and_does_not_change_state(self):
        before = self.repository.load()
        status, result = self.invoke("process", "--limit", "0")
        self.assertEqual(status, 1)
        self.assertEqual(result["code"], "invalid_input")
        self.assertEqual(self.repository.load(), before)

    def test_rehearsal_does_not_open_the_application_database_or_replace_evidence(self):
        destination = Path(self.workspace.name) / "reliability.json"
        report = {"passed": True, "simulation": True}
        for expected in (0, 1):
            with patch("orderflow.reliability.reliability_rehearsal", return_value=report) as rehearsal, \
                    patch("orderflow.cli.SQLiteRepository", side_effect=AssertionError("Must remain isolated")), \
                    redirect_stdout(StringIO()) as output:
                self.assertEqual(main(["rehearse", "--output", str(destination)]), expected)
                if expected == 0:
                    rehearsal.assert_called_once_with()
                    self.assertEqual(json.loads(output.getvalue()), report)
                else:
                    rehearsal.assert_not_called()
                    self.assertIn("already exists", json.loads(output.getvalue())["error"])
            self.assertEqual(json.loads(destination.read_text(encoding="utf-8")), report)

    def test_corrupt_restore_reports_error_and_retains_live_state_and_safety_backup(self):
        candidate = self.repository.backup(Path(self.workspace.name) / "candidate.db")
        SQLiteRepository(candidate).transact(lambda state: state["inventory"].update({"TOTE-001": None}))
        before = self.repository.load()
        status, result = self.invoke("restore", str(candidate))
        self.assertEqual(status, 1)
        self.assertEqual(result["code"], "local_error")
        self.assertEqual(self.repository.load(), before)
        backups = list(self.database.parent.glob("orders-before-restore-*.db"))
        self.assertEqual(len(backups), 1)
        validate_state(SQLiteRepository(backups[0]).load())

    def test_cancellation_discards_unpublished_work_and_late_delivery_is_ignored(self):
        order, _ = self.engine.submit({"customer": "Cancelled studio", "items": [{"sku": "TOTE-001", "quantity": 2}],
                                      "idempotency_key": "cancel-request-001"})
        self.engine.cancel(order["id"])
        state = self.repository.load()
        self.assertEqual({row["status"] for row in state["outbox"].values()}, {"discarded"})
        self.assertEqual(self.engine.operations()["outbox_pending"], 0)
        self.assertEqual(self.engine.process_order(order["id"], expected_stage="payment")["outcome"], "ignored")
        self.assertEqual(self.repository.load(), state)
        validate_state(state)


if __name__ == "__main__":
    unittest.main()
