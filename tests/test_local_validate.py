"""Local ML rehearsal checks use job artifacts while refusing cloud access."""
import copy
from contextlib import redirect_stderr
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from aws import jobs
from aws import local_validate


def fixture():
    return {"source": "synthetic", "source_label": "Synthetic test fixture", "start_date": "2020-01-01",
            "total_days": 224, "metadata": {}, "series": [
                {"store_id": "CA_1", "item_id": "FOODS_1_001", "category": "FOODS", "department": "FOODS_1",
                 "name": "Test item", "price": 4, "values": [4, 6, 8, 10, 12, 14, 16] * 32}]}


HAS_ML = importlib.util.find_spec("xgboost") is not None and importlib.util.find_spec("numpy") is not None


class LocalOutputSafetyTests(unittest.TestCase):
    def test_existing_artifacts_are_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "dataset.json"
            jobs.write_json(source, fixture())
            output = root / "existing"
            output.mkdir()
            marker = output / "keep.txt"
            marker.write_text("existing artifact", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "new or empty"):
                local_validate.run_rehearsal(source, output)
            self.assertEqual(marker.read_text(encoding="utf-8"), "existing artifact")

    def test_output_cannot_contain_source_and_invalid_cutoff_creates_nothing(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "dataset.json"
            jobs.write_json(source, fixture())
            with self.assertRaisesRegex(ValueError, "must not contain"):
                local_validate.run_rehearsal(source, root)
            output = root / "invalid-cutoff"
            with self.assertRaisesRegex(ValueError, "Replay cutoff"):
                local_validate.run_rehearsal(source, output, 197)
            self.assertFalse(output.exists())

    def test_malformed_input_is_rejected_before_output_creation(self):
        malformed = [None, [], {**fixture(), "source": "custom"}, {**fixture(), "source_label": " "},
                     {**fixture(), "total_days": "224"}, {**fixture(), "total_days": True},
                     {**fixture(), "start_date": "20200101"}, {**fixture(), "start_date": "invalid"},
                     {**fixture(), "series": None}, {**fixture(), "series": [None]},
                     {**fixture(), "series": [{**fixture()["series"][0], "values": {}}]}]
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, output = root / "dataset.json", root / "artifacts"
            for value in malformed:
                with self.subTest(value=value):
                    jobs.write_json(source, value)
                    with self.assertRaises(ValueError):
                        local_validate.run_rehearsal(source, output)
                    self.assertFalse(output.exists())

    def test_duplicate_identity_is_rejected_before_output_creation(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            value = fixture()
            value["series"].append(copy.deepcopy(value["series"][0]))
            source, output = root / "dataset.json", root / "artifacts"
            jobs.write_json(source, value)
            with self.assertRaisesRegex(ValueError, "Duplicate"):
                local_validate.run_rehearsal(source, output)
            self.assertFalse(output.exists())

    def test_cli_malformed_dataset_exits_cleanly_without_a_report(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, output = root / "dataset.json", root / "artifacts"
            jobs.write_json(source, None)
            stderr = io.StringIO()
            with redirect_stderr(stderr), self.assertRaises(SystemExit) as error:
                local_validate.main(["--dataset", str(source), "--output", str(output)])
            self.assertEqual(error.exception.code, 2)
            self.assertIn("Dataset must be a JSON object", stderr.getvalue())
            self.assertNotIn("Traceback", stderr.getvalue())
            self.assertFalse(output.exists())


@unittest.skipUnless(HAS_ML, "Install the ml extra for the two-candidate rehearsal")
class LocalRehearsalTests(unittest.TestCase):
    def test_real_stages_archives_and_no_aws_sdk(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "dataset.json"
            jobs.write_json(source, fixture())
            with patch.dict(sys.modules, {"boto3": None}), \
                    patch.object(jobs, "prepare", wraps=jobs.prepare) as prepare, \
                    patch.object(jobs, "train", wraps=jobs.train) as train, \
                    patch.object(jobs, "evaluate", wraps=jobs.evaluate) as evaluate:
                report = local_validate.run_rehearsal(source, root / "artifacts")
            self.assertEqual((prepare.call_count, train.call_count, evaluate.call_count), (2, 2, 2))
            self.assertTrue(report["checks_passed"])
            self.assertEqual(report["unique_forecast_count"], 2)
            self.assertEqual(report["forecast_count"], 3)
            self.assertEqual(report["policy_simulation_count"], 27)
            self.assertEqual(report["aws_execution"], {"model_registry": False, "batch_transform": False,
                                                       "publication": False})
            self.assertEqual({run["candidate"] for run in report["runs"]}, {"seasonal", "xgboost"})
            seasonal = next(run for run in report["runs"] if run["candidate"] == "seasonal")
            self.assertTrue(seasonal["release_gate"]["passed"])
            for run in report["runs"]:
                directory = root / "artifacts" / run["candidate"]
                self.assertEqual(jobs.load_bundle(directory / "model"), jobs.load_bundle(directory / "trained-model"))
                self.assertTrue((root / "artifacts" / run["forecasts"]).is_file())
            self.assertEqual(json.loads((root / "artifacts/report.json").read_text(encoding="utf-8")), report)

    def test_changed_final_holdout_does_not_change_model_or_forecast(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "dataset.json"
            value = fixture()
            jobs.write_json(source, value)
            local_validate.run_rehearsal(source, root / "first")
            changed = copy.deepcopy(value)
            changed["series"][0]["values"][196:] = [999999] * 28
            jobs.write_json(source, changed)
            local_validate.run_rehearsal(source, root / "changed")
            for candidate in ("seasonal", "xgboost"):
                first, second = root / "first" / candidate, root / "changed" / candidate
                self.assertEqual(jobs.read_json(first / "trained-model/bundle.json"),
                                 jobs.read_json(second / "trained-model/bundle.json"))
                self.assertEqual((first / "transform/requests.jsonl.out").read_text(),
                                 (second / "transform/requests.jsonl.out").read_text())

    def test_zero_scored_demand_is_a_rejection_not_a_failed_rehearsal(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            value = fixture()
            value["series"][0]["values"] = [0] * 224
            source = root / "dataset.json"
            jobs.write_json(source, value)
            report = local_validate.run_rehearsal(source, root / "artifacts")
            self.assertTrue(report["checks_passed"])
            self.assertTrue(all(not run["release_gate"]["passed"] and run["comparison_only"]
                                for run in report["runs"]))
            self.assertTrue(all(run["release_gate"]["wape"] is None for run in report["runs"]))

    def test_inference_that_refits_fails_rehearsal(self):
        def retraining_inference(bundle, request):
            history = bundle["dataset"]
            series = history["series"][0]
            # Resolve the patched entry point at call time rather than keeping the real fit function.
            from retail_forecast.forecast import train_artifact as current_train
            current_train(series, history["start_date"], history["cutoff"], request["model"])

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "dataset.json"
            jobs.write_json(source, fixture())
            with patch.object(local_validate, "infer", side_effect=retraining_inference):
                with self.assertRaisesRegex(RuntimeError, "attempted to fit"):
                    local_validate.run_rehearsal(source, root / "artifacts")
            self.assertFalse((root / "artifacts/report.json").exists())

    def test_nonfinite_inference_cannot_write_a_success_report(self):
        real_infer = local_validate.infer

        def nonfinite_inference(bundle, request):
            result = real_infer(bundle, request)
            result["forecast"][0]["p50"] = float("nan")
            return result

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "dataset.json"
            jobs.write_json(source, fixture())
            with patch.object(local_validate, "infer", side_effect=nonfinite_inference):
                with self.assertRaisesRegex(ValueError, "non-finite"):
                    local_validate.run_rehearsal(source, root / "artifacts")
            self.assertFalse((root / "artifacts/report.json").exists())
            prediction_file = root / "artifacts/xgboost/transform/requests.jsonl.out"
            self.assertEqual(prediction_file.read_text(encoding="utf-8"), "")


if __name__ == "__main__":
    unittest.main()
