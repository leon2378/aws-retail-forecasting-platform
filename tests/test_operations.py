"""Recovery and visibility checks use generated fixtures, never bundled M5 data."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from retail_forecast.api import handle_request
from retail_forecast.data import demo_dataset
from retail_forecast.operations import CheckpointStore, InterruptedBatch, LocalOperations
from retail_forecast.storage import DynamoRepository, LocalRepository, build_forecast_payload


def fixture():
    dataset = demo_dataset()
    dataset["total_days"] = 252
    dataset["series"] = dataset["series"][:3]
    for series in dataset["series"]:
        series["values"] = series["values"][:252]
    return dataset


class OperationsTests(unittest.TestCase):
    def test_drill_executes_isolated_failures_and_restores_exact_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset = fixture()
            source = json.dumps(dataset).encode()
            (root / "dataset.json").write_bytes(source)
            (root / "releases.json").write_text("[]")
            repository = LocalRepository(root)
            series = dataset["series"][0]
            before = repository.forecast(series["store_id"], series["item_id"], 224, "seasonal")
            snapshot_before = repository.operations()["summary"]["snapshot_hash"]
            status, result = handle_request("POST", "/api/operations/drill", {}, {"request_token": "test-drill"}, repository)
            self.assertEqual(status, 200)
            drill = result["latest_drill"]
            self.assertEqual(drill["status"], "completed", drill)
            self.assertTrue(all(step["status"] == "passed" for step in drill["steps"]))
            for key in ("invalid_snapshot_blocked", "interrupted_batch_hidden", "retry_idempotent", "rollback_restored"):
                self.assertTrue(drill["proof"][key])
            self.assertEqual(drill["proof"]["forecast_hash_before"], drill["proof"]["forecast_hash_after"])
            self.assertGreater(drill["recovery_seconds"], 0)
            self.assertEqual(result["summary"]["snapshot_hash"], snapshot_before)
            self.assertEqual(repository.forecast(series["store_id"], series["item_id"], 224, "seasonal"), before)
            self.assertEqual((root / "dataset.json").read_bytes(), source)
            self.assertEqual((root / "releases.json").read_text(), "[]")
            restarted = LocalRepository(root)
            self.assertEqual(restarted.operations()["latest_drill"], drill)
            self.assertEqual(restarted.recovery_drill("test-drill")["latest_drill"], drill)
            self.assertNotIn(str(root), json.dumps(result))
            self.assertNotIn('"values"', json.dumps(result))
            json.dumps(result, allow_nan=False)

    def test_partial_batch_restart_and_idempotent_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            dataset = fixture()
            store = LocalOperations(temporary, build_forecast_payload)
            store.initialize(dataset)
            before = store.serve_bytes()
            with self.assertRaises(InterruptedBatch):
                store.publish(dataset, 224, "retry", interrupt=True)
            restarted = LocalOperations(temporary, build_forecast_payload)
            self.assertEqual(restarted.serve_bytes(), before)
            self.assertEqual(restarted.status()["runs"][0]["status"], "interrupted")
            first = restarted.publish(dataset, 224, "retry")
            after = restarted.serve_bytes()
            second = LocalOperations(temporary, build_forecast_payload).publish(dataset, 224, "retry")
            self.assertTrue(first["committed"])
            self.assertTrue(second["idempotent"])
            self.assertEqual(restarted.serve_bytes(), after)
            self.assertEqual(len([run for run in restarted.status()["runs"] if run["id"] == first["version"]]), 1)

    def test_crash_after_manifest_before_commit_is_not_recorded_as_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            from retail_forecast import operations
            dataset = fixture()
            store = LocalOperations(temporary, build_forecast_payload)
            store.initialize(dataset)
            before = store.serve_bytes()
            original = operations._write
            def crash_before_pointer(path, raw):
                if path.name == "active.json":
                    raise SystemExit("injected process interruption")
                return original(path, raw)
            with patch("retail_forecast.operations._write", side_effect=crash_before_pointer):
                with self.assertRaises(SystemExit):
                    store.publish(dataset, 224, "commit-retry")
            restarted = LocalOperations(temporary, build_forecast_payload)
            self.assertEqual(restarted.serve_bytes(), before)
            self.assertEqual(restarted.status()["runs"][0]["status"], "running")
            result = restarted.publish(dataset, 224, "commit-retry")
            self.assertTrue(result["committed"])
            self.assertFalse(result["idempotent"])
            self.assertEqual(restarted.active()["manifest"]["version"], result["version"])

    def test_source_membership_failure_quarantined_and_prior_served_after_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset = fixture()
            (root / "dataset.json").write_text(json.dumps(dataset))
            repository = LocalRepository(root)
            before = repository._operations.serve_bytes()
            invalid = copy.deepcopy(dataset)
            invalid["series"].pop()
            invalid_bytes = json.dumps(invalid).encode()
            (root / "dataset.json").write_bytes(invalid_bytes)
            restarted = LocalRepository(root)
            self.assertEqual(len(restarted.dataset["series"]), 3)
            self.assertEqual(restarted._operations.serve_bytes(), before)
            self.assertEqual((root / "dataset.json").read_bytes(), invalid_bytes)
            summary = restarted.operations()
            self.assertEqual(summary["summary"]["status"], "attention")
            self.assertFalse(summary["quality"]["passed"])
            self.assertTrue(list((root / "operations" / "quarantine").glob("*/source.json")))

    def test_crash_after_atomic_pointer_commit_is_verified_on_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            from retail_forecast import operations
            dataset = fixture()
            store = LocalOperations(temporary, build_forecast_payload)
            store.publish(dataset, 223, "baseline")
            original = operations._write
            def crash_after_pointer(path, raw):
                if path.name == "committed.json":
                    raise SystemExit("injected process interruption")
                return original(path, raw)
            with patch("retail_forecast.operations._write", side_effect=crash_after_pointer):
                with self.assertRaises(SystemExit):
                    store.publish(dataset, 224, "acknowledgement-retry")
            restarted = LocalOperations(temporary, build_forecast_payload)
            visible = restarted.serve_bytes()
            self.assertEqual(len(json.loads(visible)), 3)
            result = restarted.publish(dataset, 224, "acknowledgement-retry")
            self.assertTrue(result["idempotent"])
            self.assertEqual(restarted.serve_bytes(), visible)
            self.assertEqual(restarted.status()["runs"][0]["status"], "completed")

    def test_malformed_source_and_no_baseline_fail_cleanly(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "dataset.json").write_bytes(b"{invalid JSON")
            with self.assertRaisesRegex(ValueError, "no verified previous"):
                LocalRepository(root)
            self.assertFalse((root / "operations" / "active.json").exists())

    def test_invalid_numeric_candidate_preserves_verified_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            dataset = fixture()
            repository = LocalRepository(temporary, dataset=dataset)
            invalid = copy.deepcopy(dataset)
            invalid["series"][0]["values"][0] = float("nan")
            restarted = LocalRepository(temporary, dataset=invalid)
            self.assertEqual(restarted.dataset, dataset)
            self.assertFalse(restarted.operations()["quality"]["passed"])
            json.dumps(restarted.operations(), allow_nan=False)

    def test_explicit_contract_approval_preserves_prior_versions(self):
        with tempfile.TemporaryDirectory() as temporary:
            first = fixture()
            repository = LocalRepository(temporary, dataset=first)
            version = repository.operations()["summary"]["active_version"]
            changed = copy.deepcopy(first)
            changed["series"].pop()
            rejected = LocalRepository(temporary, dataset=changed)
            self.assertEqual(rejected.operations()["summary"]["active_version"], version)
            accepted = LocalRepository(temporary, dataset=changed, approve_contract=True)
            self.assertEqual(len(accepted.dataset["series"]), 2)
            self.assertNotEqual(accepted.operations()["summary"]["active_version"], version)
            self.assertTrue((Path(temporary) / "operations" / "snapshots" / version / "manifest.json").exists())
            self.assertTrue(any(event["type"] == "contract_approved" for event in accepted.operations()["events"]))

    def test_new_arrival_growth_advances_replay_clock_without_calendar_staleness(self):
        with tempfile.TemporaryDirectory() as temporary:
            dataset = fixture()
            LocalRepository(temporary, dataset=dataset)
            grown = copy.deepcopy(dataset)
            grown["total_days"] += 1
            for series in grown["series"]:
                series["values"].append(2)
            repository = LocalRepository(temporary, dataset=grown)
            status = repository.operations()
            self.assertEqual(status["summary"]["active_cutoff"], 225)
            self.assertEqual(status["summary"]["expected_cutoff"], 225)
            self.assertEqual(status["summary"]["replay_lag_days"], 0)
            self.assertEqual(status["summary"]["status"], "healthy")

    def test_rollback_rejects_missing_artifact_and_keeps_current_visible(self):
        with tempfile.TemporaryDirectory() as temporary:
            dataset = fixture()
            store = LocalOperations(temporary, build_forecast_payload)
            store.publish(dataset, 223, "baseline")
            pointer = store.active()["pointer"]
            store.publish(dataset, 224, "candidate")
            before = store.serve_bytes()
            (Path(temporary) / "snapshots" / pointer["version"] / "forecasts.json").unlink()
            with self.assertRaisesRegex(ValueError, "integrity"):
                store.rollback(pointer)
            self.assertEqual(store.serve_bytes(), before)

    def test_restart_rejects_hash_corruption_and_never_serves_unverified_payload(self):
        with tempfile.TemporaryDirectory() as temporary:
            dataset = fixture()
            repository = LocalRepository(temporary, dataset=dataset)
            pointer = repository._operations.active()["pointer"]
            artifact = Path(temporary) / "operations" / "snapshots" / pointer["version"] / "forecasts.json"
            artifact.write_bytes(artifact.read_bytes() + b" ")
            self.assertEqual(repository.operations()["summary"]["status"], "attention")
            with self.assertRaisesRegex(ValueError, "integrity"):
                LocalRepository(temporary, dataset=dataset)
            first = dataset["series"][0]
            status, _ = handle_request("GET", "/api/forecast", {"store": first["store_id"], "item": first["item_id"]}, None, repository)
            self.assertEqual(status, 400)

    def test_concurrent_repository_writers_rejected_and_status_refreshes(self):
        with tempfile.TemporaryDirectory() as temporary:
            dataset = fixture()
            first = LocalOperations(temporary, build_forecast_payload)
            first.initialize(dataset)
            second = LocalOperations(temporary, build_forecast_payload)
            with first.writer():
                with self.assertRaisesRegex(ValueError, "already writing"):
                    second.publish(dataset, 224, "concurrent")
            first.event("test_event", "A persisted audit update.")
            self.assertTrue(any(event["type"] == "test_event" for event in second.status()["events"]))
            self.assertTrue(second.publish(dataset, 224, "serialized")["committed"])
            self.assertTrue(first.publish(dataset, 224, "serialized")["idempotent"])

    def test_drill_api_rejects_user_paths_and_bad_tokens(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository = LocalRepository(temporary, dataset=fixture())
            for body in (None, [], {"directory": "outside"}, {"request_token": "../escape"},
                         {"request_token": True}, {"request_token": False}, {"request_token": ""}, {"request_token": 0}):
                status, _ = handle_request("POST", "/api/operations/drill", {}, body, repository)
                self.assertEqual(status, 400)
            status, _ = handle_request("POST", "/api/operations/drill", {}, {}, DynamoRepository(table=object()))
            self.assertEqual(status, 403)

    def test_existing_server_refreshes_dataset_after_explicit_cli_import(self):
        with tempfile.TemporaryDirectory() as temporary:
            dataset = fixture()
            server = LocalRepository(temporary, dataset=dataset)
            changed = copy.deepcopy(dataset)
            changed["series"].pop()
            LocalRepository(temporary, dataset=changed, approve_contract=True)
            self.assertEqual(len(server.catalog()["products"]), 2)
            self.assertEqual(server.dataset, changed)

    def test_transactional_import_rejects_invalid_without_replacing_source_or_pointer(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "dataset.json"
            original = fixture()
            repository = LocalRepository.import_snapshot(path, original)
            before_bytes = path.read_bytes()
            before_hash = repository.operations()["summary"]["snapshot_hash"]
            invalid = copy.deepcopy(original)
            invalid["series"][0]["values"][0] = -1
            with self.assertRaisesRegex(ValueError, "quality gate"):
                LocalRepository.import_snapshot(path, invalid)
            self.assertEqual(path.read_bytes(), before_bytes)
            self.assertEqual(repository.operations()["summary"]["snapshot_hash"], before_hash)

    def test_source_write_failure_rolls_back_checkpoint_and_preserves_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            from retail_forecast import operations
            path = Path(temporary) / "dataset.json"
            original = fixture()
            repository = LocalRepository.import_snapshot(path, original)
            before_bytes = path.read_bytes()
            before_forecasts = repository._operations.serve_bytes()
            before_pointer = repository._operations.active()["pointer"]
            changed = copy.deepcopy(original)
            changed["series"].pop()
            write = operations._write
            def fail_source(target, raw):
                if target == path:
                    self.assertEqual(repository._operations.active()["pointer"], before_pointer)
                    raise OSError("injected source replacement failure")
                return write(target, raw)
            with patch("retail_forecast.operations._write", side_effect=fail_source):
                with self.assertRaisesRegex(ValueError, "previous forecast was restored"):
                    LocalRepository.import_snapshot(path, changed)
            restarted = LocalRepository(temporary)
            self.assertEqual(path.read_bytes(), before_bytes)
            self.assertEqual(restarted._operations.serve_bytes(), before_forecasts)
            self.assertEqual(restarted._operations.active()["pointer"], before_pointer)
            self.assertTrue(any(event["type"] == "source_import_failed" for event in restarted.operations()["events"]))

    def test_import_pointer_failure_restores_exact_original_source_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            from retail_forecast import operations
            path = Path(temporary) / "dataset.json"
            original = fixture()
            repository = LocalRepository.import_snapshot(path, original)
            before_bytes = path.read_bytes()
            before_pointer = repository._operations.active()["pointer"]
            changed = copy.deepcopy(original)
            changed["series"].pop()
            write = operations._write
            def fail_pointer(target, raw):
                if target.name == "active.json":
                    self.assertEqual(json.loads(path.read_bytes()), changed)
                    raise OSError("injected pointer commit failure")
                return write(target, raw)
            with patch("retail_forecast.operations._write", side_effect=fail_pointer):
                with self.assertRaisesRegex(ValueError, "previous forecast was restored"):
                    LocalRepository.import_snapshot(path, changed)
            self.assertEqual(path.read_bytes(), before_bytes)
            self.assertEqual(repository._operations.active()["pointer"], before_pointer)

    def test_import_commit_failure_removes_only_new_source_when_no_prior_exists(self):
        with tempfile.TemporaryDirectory() as temporary:
            from retail_forecast import operations
            path = Path(temporary) / "dataset.json"
            write = operations._write
            def fail_pointer(target, raw):
                if target.name == "active.json":
                    raise OSError("injected pointer commit failure")
                return write(target, raw)
            with patch("retail_forecast.operations._write", side_effect=fail_pointer):
                with self.assertRaisesRegex(ValueError, "previous forecast was restored"):
                    LocalRepository.import_snapshot(path, fixture())
            self.assertFalse(path.exists())
            self.assertFalse((Path(temporary) / "operations" / "active.json").exists())

    def test_explicit_reimport_can_restore_an_earlier_dataset_contract(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "dataset.json"
            original = fixture()
            LocalRepository.import_snapshot(path, original)
            changed = copy.deepcopy(original)
            changed["series"].pop()
            LocalRepository.import_snapshot(path, changed)
            restored = LocalRepository.import_snapshot(path, original)
            self.assertEqual(len(restored.catalog()["products"]), 3)
            self.assertEqual(restored._operations.active()["dataset"], original)
            self.assertEqual(json.loads(path.read_bytes()), original)

    def test_explicit_import_supports_custom_output_filename(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "selected-products.json"
            dataset = fixture()
            repository = LocalRepository.import_snapshot(path, dataset)
            self.assertEqual(json.loads(path.read_bytes()), dataset)
            self.assertEqual(repository.dataset, dataset)
            self.assertTrue(repository.operations()["quality"]["passed"])


class OperationsTable:
    def __init__(self, published=False, pending=False):
        self.reads = []
        self.items = {("REPLAY", "STATE"): {"cutoff": 224}}
        if published:
            self.items[("CATALOG", "META")] = {"payload": {
                "source": "synthetic", "source_label": "Generated test fixture",
                "start_date": "2014-01-01", "default_cutoff": 224,
                "products": [{"store_id": "CA_1", "item_id": "A"}],
                "published_at": "2026-10-08T01:00:00+00:00", "published_run_id": "run-1",
                "snapshot_sha256": "a" * 64}}
            self.items[("COMPLETED", "000000224")] = {"models": ["seasonal"], "run_id": "run-1"}
        if pending:
            self.items[("REPLAY", "STATE")].update({"pending_cutoff": 225, "run_id": "pending-run",
                "started_at": "2026-10-08T02:00:00+00:00"})

    def get_item(self, Key, **kwargs):
        self.reads.append(Key["pk"])
        item = self.items.get((Key["pk"], Key["sk"]))
        return {"Item": copy.deepcopy(item)} if item else {}

    def query(self, **kwargs):
        self.reads.append("RELEASES")
        return {"Items": []}


class AWSOperationsTests(unittest.TestCase):
    def test_empty_cloud_operations_have_unknown_quality_and_no_local_fallback(self):
        table = OperationsTable()
        status, result = handle_request("GET", "/api/operations", {}, None, DynamoRepository(table=table))
        self.assertEqual(status, 200)
        self.assertEqual(result["summary"]["status"], "awaiting_publication")
        self.assertIsNone(result["quality"]["passed"])
        self.assertIsNone(result["source"])
        self.assertFalse(result["capabilities"]["can_run_drill"])
        self.assertEqual(set(table.reads), {"CATALOG", "REPLAY", "RELEASES"})

    def test_cloud_reports_only_committed_metadata_and_pending_job_unknown(self):
        table = OperationsTable(published=True, pending=True)
        result = DynamoRepository(table=table).operations()
        self.assertEqual(result["summary"]["replay_lag_days"], 1)
        self.assertEqual(result["summary"]["active_version"], "run-1")
        self.assertEqual(result["summary"]["snapshot_hash"], "a" * 64)
        self.assertEqual(result["runs"][0]["status"], "pending")
        self.assertIn("unknown", result["runs"][0]["reason"])
        self.assertEqual(set(table.reads), {"CATALOG", "REPLAY", "COMPLETED", "RELEASES"})
        json.dumps(result, allow_nan=False)

    def test_cloud_mismatched_completion_marker_is_attention(self):
        table = OperationsTable(published=True)
        table.items[("COMPLETED", "000000224")]["run_id"] = "wrong-run"
        result = DynamoRepository(table=table).operations()
        self.assertEqual(result["summary"]["status"], "attention")
        self.assertIsNone(result["summary"]["active_cutoff"])


if __name__ == "__main__":
    unittest.main()
