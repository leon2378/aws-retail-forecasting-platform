"""Regressions for credential-free publisher/worker integration rehearsal."""

import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from orderflow.reliability import SimulatedQueue, reliability_rehearsal
from orderflow.storage import SQLiteRepository


class SimulatedQueueTests(unittest.TestCase):
    def setUp(self):
        self.queue = SimulatedQueue()

    def send(self, body="body"):
        return self.queue.send_message(QueueUrl=self.queue.queue_url, MessageBody=body)["MessageId"]

    def test_lost_send_response_still_accepts_message(self):
        self.queue.lose_next_send_acknowledgement = True
        with self.assertRaises(TimeoutError):
            self.send()
        self.assertEqual(len(self.queue.messages), 1)
        self.assertEqual(self.queue.receive_batch()[0]["body"], "body")

    def test_partial_acknowledgement_visibility_and_five_receive_redrive(self):
        good, failed = self.send("good"), self.send("failed")
        batch = self.queue.receive_batch()
        result = self.queue.acknowledge_batch(batch, {"batchItemFailures": [{"itemIdentifier": failed}]})
        self.assertEqual(result, {"deleted": [good], "retained": [failed]})
        self.assertEqual(self.queue.receive_batch(), [])
        self.queue.advance(179)
        self.assertEqual(self.queue.receive_batch(), [])
        self.queue.advance(1)
        for number in range(2, 6):
            batch = self.queue.receive_batch()
            self.assertEqual(len(batch), 1)
            self.assertEqual(batch[0]["attributes"]["ApproximateReceiveCount"], str(number))
            self.queue.acknowledge_batch(batch, {"batchItemFailures": [{"itemIdentifier": failed}]})
            if number < 5:
                self.queue.advance(180)
        self.assertEqual(self.queue.dead_letters, [])
        self.assertIn(failed, self.queue.messages)
        self.queue.advance(180)
        self.assertEqual(self.queue.receive_batch(), [])
        self.assertEqual(self.queue.messages, {})
        self.assertEqual(self.queue.dead_letters[0]["receive_count"], 5)
        replay = self.queue.replay_dead_letter()["MessageId"]
        self.assertNotEqual(replay, failed)
        self.assertEqual(self.queue.dead_letters, [])
        self.assertEqual(self.queue.receive_batch()[0]["attributes"]["ApproximateReceiveCount"], "1")

    def test_stale_receipt_cannot_delete_a_later_receive(self):
        identifier = self.send()
        old = self.queue.receive_batch()
        self.queue.advance(180)
        new = self.queue.receive_batch()
        self.assertEqual(self.queue.acknowledge_batch(old, {"batchItemFailures": []})["deleted"], [])
        self.assertIn(identifier, self.queue.messages)
        self.assertEqual(self.queue.acknowledge_batch(new, {"batchItemFailures": []})["deleted"], [identifier])

    def test_invalid_partial_batch_response_does_not_delete_anything(self):
        identifier = self.send()
        batch = self.queue.receive_batch()
        with self.assertRaises(ValueError):
            self.queue.acknowledge_batch(batch, {"batchItemFailures": [{"itemIdentifier": "not-in-batch"}]})
        self.assertIn(identifier, self.queue.messages)


class ReliabilityRehearsalTests(unittest.TestCase):
    def test_integrated_report_checks_real_end_states_and_cleanup(self):
        report = reliability_rehearsal()
        self.assertTrue(report["passed"], report)
        self.assertTrue(report["simulation"])
        self.assertEqual(report["scope"], "isolated")
        self.assertEqual(report["summary"]["scenarios"], 5)
        self.assertGreaterEqual(len(report["checks"]), 17)
        self.assertTrue(all(check["passed"] for check in report["checks"]))
        self.assertTrue(report["temporary_storage_removed"])
        self.assertEqual(report["queue_model"]["visibility_timeout_seconds"], 180)
        self.assertEqual(report["queue_model"]["max_receive_count"], 5)
        scenarios = {row["name"]: row["evidence"] for row in report["scenarios"]}
        self.assertEqual(scenarios["retry_exhaustion_and_recovery"]["domain_attempts"], [1, 2, 3, 3, 3])
        self.assertEqual(scenarios["retry_exhaustion_and_recovery"]["source_receive_counts"], [1, 2, 3, 4, 5])
        self.assertEqual(scenarios["mixed_batch"]["acknowledged_successes"], 1)
        self.assertEqual(scenarios["mixed_batch"]["retained_failures"], 2)
        json.dumps(report, allow_nan=False)

    def test_repeat_runs_are_isolated_cleaned_and_repeatable(self):
        databases = []

        def track(database):
            repository = SQLiteRepository(database)
            databases.append(repository.path)
            return repository

        with patch("orderflow.reliability.SQLiteRepository", side_effect=track):
            first = reliability_rehearsal()
            second = reliability_rehearsal()
        self.assertTrue(first["passed"], first)
        self.assertEqual(first, second)
        self.assertEqual(len(set(databases)), 10)
        self.assertTrue(all(not database.exists() and not database.parent.exists() for database in databases))

    def test_existing_database_is_never_opened_or_changed(self):
        scratch = Path(__file__).resolve().parents[1] / "build"
        scratch.mkdir(exist_ok=True)
        with TemporaryDirectory(dir=scratch) as directory:
            live = Path(directory) / "data" / "orderflow.db"
            live.parent.mkdir()
            live.write_bytes(b"Existing user database must remain untouched")
            before = live.read_bytes()
            with patch("pathlib.Path.cwd", return_value=Path(directory)):
                report = reliability_rehearsal()
            self.assertTrue(report["passed"], report)
            self.assertEqual(live.read_bytes(), before)
            self.assertEqual(set(Path(directory).iterdir()), {live.parent, Path(directory) / "build"})
            self.assertEqual(list((Path(directory) / "build").iterdir()), [])

    def test_rehearsal_runs_when_aws_sdk_imports_are_unavailable(self):
        script = """
import json
import sys
from orderflow.reliability import reliability_rehearsal
assert "aws.handlers.outbox" not in sys.modules
assert "aws.handlers.worker" not in sys.modules
report = reliability_rehearsal()
assert "boto3" not in sys.modules and "botocore" not in sys.modules
assert "aws.repository" not in sys.modules
print(json.dumps({"passed": report["passed"], "cleaned": report["temporary_storage_removed"]}))
"""
        # -S starts without installed site packages, including the optional SDK.
        result = subprocess.run([sys.executable, "-S", "-c", script],
                                cwd=Path(__file__).resolve().parents[1],
                                text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"passed": True, "cleaned": True})
        self.assertEqual(result.stderr, "")

    def test_worker_regression_returns_failed_evidence(self):
        with patch("aws.handlers.worker.process_records", return_value={"batchItemFailures": []}):
            report = reliability_rehearsal()
        self.assertFalse(report["passed"])
        self.assertEqual(report["status"], "failed")
        self.assertTrue(any(not check["passed"] for check in report["checks"]))
        self.assertTrue(report["temporary_storage_removed"])

    def test_publisher_regression_returns_failed_evidence(self):
        with patch("aws.handlers.outbox.publish", return_value={"sent": 1, "failed": [], "pending": 0}):
            report = reliability_rehearsal()
        self.assertFalse(report["passed"])
        self.assertTrue(any(not row["passed"] for row in report["scenarios"]))
        self.assertTrue(report["temporary_storage_removed"])


if __name__ == "__main__":
    unittest.main()
