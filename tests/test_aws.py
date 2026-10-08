"""Offline checks of the real job entry points, graph and replay safeguards."""
import argparse
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from aws import jobs
from aws.handlers import ingest
from aws.pipeline import build_pipeline
from aws.serve import infer


def dataset():
    return {"source": "synthetic", "source_label": "Synthetic test fixture", "start_date": "2020-01-01",
            "total_days": 252, "metadata": {}, "series": [
                {"store_id": "CA_1", "item_id": "FOODS_1_001", "category": "FOODS", "department": "FOODS_1",
                 "name": "Test item", "price": 4, "values": [4, 6, 8, 10, 12, 14, 16] * 36}]}


class PipelineTests(unittest.TestCase):
    def definition(self, name="retail-forecast"):
        return build_pipeline(name=name, role_arn="arn:aws:iam::123456789012:role/test",
                              image_uri="123456789012.dkr.ecr.ap-southeast-2.amazonaws.com/test:1",
                              bucket="test-data", table_name="test-results", model_group=name)

    def test_training_and_release_gate_cannot_receive_final_scoring_labels(self):
        definition = self.definition()
        prepare, train, evaluate, gate = definition["Steps"]
        channels = train["Arguments"]["InputDataConfig"]
        self.assertEqual([c["ChannelName"] for c in channels], ["history"])
        self.assertIn("Steps.Prepare", str(channels[0]))
        self.assertEqual([i["InputName"] for i in evaluate["Arguments"]["ProcessingInputs"]], ["model"])
        self.assertEqual(gate["Arguments"]["Conditions"][0]["LeftValue"]["Std:JsonGet"]["Path"], "passed")
        approved = gate["Arguments"]["IfSteps"]
        rejected = gate["Arguments"]["ElseSteps"]
        self.assertEqual([s["Name"] for s in approved], ["RegisterApproved", "CreateBatchModel", "DailyForecast", "Publish"])
        self.assertEqual([s["Name"] for s in rejected], ["RegisterRejected", "Reject"])
        self.assertEqual(approved[0]["Arguments"]["ModelApprovalStatus"], "Approved")
        self.assertEqual(rejected[0]["Arguments"]["ModelApprovalStatus"], "Rejected")
        self.assertEqual(approved[1]["Arguments"]["PrimaryContainer"]["ModelPackageName"],
                         {"Get": "Steps.RegisterApproved.ModelPackageArn"})
        inputs = {i["InputName"]: i for i in approved[-1]["Arguments"]["ProcessingInputs"]}
        self.assertEqual(set(inputs), {"forecast", "labels", "catalog", "evaluation"})
        self.assertIn("Steps.DailyForecast.TransformOutput", str(inputs["forecast"]))
        self.assertEqual(prepare["Arguments"]["ProcessingInputs"][0]["S3Input"]["S3Uri"], {"Get": "Parameters.SnapshotUri"})
        self.assertIn("quality", [output["OutputName"] for output in prepare["Arguments"]["ProcessingOutputConfig"]["Outputs"]])

    def test_job_names_fit_service_limit_and_run_id_is_required(self):
        definition = self.definition("a" * 21)
        parameters = {p["Name"]: p for p in definition["Parameters"]}
        self.assertNotIn("DefaultValue", parameters["RunId"])
        gate = definition["Steps"][-1]
        steps = definition["Steps"][:-1] + gate["Arguments"]["IfSteps"] + gate["Arguments"]["ElseSteps"]
        names = []
        for step in steps:
            for key, value in step["Arguments"].items():
                if key in {"ProcessingJobName", "TrainingJobName", "ModelName", "TransformJobName"} and "Std:Join" in value:
                    rendered = "".join(v if isinstance(v, str) else "f" * 32 for v in value["Std:Join"]["Values"])
                    names.append(rendered)
        self.assertTrue(names)
        self.assertTrue(all(len(n) <= 63 for n in names))
        for invalid in ["a" * 22, "bad-", "AProject", "ab"]:
            with self.assertRaises(ValueError):
                self.definition(invalid)


class JobIntegrationTests(unittest.TestCase):
    def test_prepare_quarantines_invalid_snapshot_before_generating_training_channels(self):
        from retail_forecast.quality import SnapshotQualityError
        for raw in (b"malformed private observations", json.dumps({**dataset(), "series": []}).encode()):
            with tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                source = root / "input/source/dataset.json"
                source.parent.mkdir(parents=True)
                source.write_bytes(raw)
                output = root / "output"
                with patch.object(jobs, "INPUT", root / "input"), patch.object(jobs, "OUTPUT", output):
                    with self.assertRaises(SnapshotQualityError):
                        jobs.prepare(argparse.Namespace(cutoff=196, model="seasonal"))
                report = jobs.read_json(output / "quality/quality.json")
                self.assertFalse(report["passed"])
                self.assertFalse((output / "history").exists())
                self.assertFalse((output / "labels").exists())
                self.assertFalse((output / "requests").exists())
                self.assertNotIn("private observations", json.dumps(report, allow_nan=False))

    def test_prepare_train_evaluate_and_infer_use_only_observed_history(self):
        source = dataset()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            input_path, output_path = root / "input", root / "output"
            jobs.write_json(input_path / "source/dataset.json", source)
            with patch.object(jobs, "INPUT", input_path), patch.object(jobs, "OUTPUT", output_path):
                jobs.prepare(argparse.Namespace(cutoff=196, model="seasonal"))
            history = jobs.read_json(output_path / "history/dataset.json")
            labels = jobs.read_json(output_path / "labels/labels.json")
            self.assertEqual(len(history["series"][0]["values"]), 196)
            self.assertEqual(labels["CA_1#FOODS_1_001"], source["series"][0]["values"][196:224])
            self.assertEqual(len(source["series"][0]["values"]), 252)
            model_path = root / "model"
            with patch.dict(os.environ, {"SM_CHANNEL_HISTORY": str(output_path / "history"), "SM_MODEL_DIR": str(model_path)}):
                jobs.train()
            bundle = jobs.load_bundle(model_path)
            report = jobs.evaluate_bundle(bundle)
            self.assertTrue(report["passed"])
            self.assertEqual(report["wape"], 0)
            request = json.loads((output_path / "requests/requests.jsonl").read_text().splitlines()[0])
            with patch("retail_forecast.forecast.train_artifact", side_effect=AssertionError("Inference must not train")):
                result = infer(bundle, request)
            self.assertEqual(len(result["forecast"]), 28)
            self.assertNotIn("_actuals", result)
            self.assertEqual(result["history"][-1]["date"], "2020-07-14")
            self.assertEqual([r["p50"] for r in result["forecast"]], labels["CA_1#FOODS_1_001"])
            # Invalid future labels have no influence on fitting or release evaluation.
            changed = copy.deepcopy(source)
            changed["series"][0]["values"][196:] = [999999] * 56
            changed_history, _ = jobs.split_snapshot(changed, 196)
            self.assertEqual(changed_history, history)

    def test_split_rejects_invalid_cutoffs_and_duplicate_identities(self):
        for cutoff in [True, 195, 225, 196.5]:
            with self.assertRaises(ValueError):
                jobs.split_snapshot(dataset(), cutoff)
        source = dataset()
        source["series"].append(copy.deepcopy(source["series"][0]))
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            jobs.split_snapshot(source, 196)

    def test_gate_rejects_measured_degradation_and_zero_scored_demand(self):
        from retail_forecast.forecast import train_artifact
        history, _ = jobs.split_snapshot(dataset(), 196)
        artifact = train_artifact(history["series"][0], history["start_date"], 196)
        bundle = {"dataset": history, "artifacts": {"CA_1#FOODS_1_001": {"seasonal": artifact}}}
        self.assertTrue(jobs.evaluate_bundle(bundle)["passed"])
        artifact["analysis"]["metrics"]["wape"] = 0.5
        self.assertFalse(jobs.evaluate_bundle(bundle)["passed"])
        history["series"][0]["values"] = [0] * 196
        self.assertFalse(jobs.evaluate_bundle(bundle)["passed"])


HAS_AWS = importlib.util.find_spec("boto3") is not None
if HAS_AWS:
    from boto3.dynamodb.types import TypeDeserializer
    from botocore.exceptions import ClientError


def error(code):
    return ClientError({"Error": {"Code": code, "Message": "Test failure"}}, "TestOperation")


class Table:
    def __init__(self):
        self.rows = {}
        self.writes = 0

    def get_item(self, Key, **kwargs):
        row = self.rows.get((Key["pk"], Key["sk"]))
        return {"Item": copy.deepcopy(row)} if row is not None else {}

    def put_item(self, Item, **kwargs):
        self.rows[(Item["pk"], Item["sk"])] = copy.deepcopy(Item)
        self.writes += 1

    def batch_writer(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def update_item(self, Key, UpdateExpression, ConditionExpression, ExpressionAttributeValues=None):
        row = self.rows.setdefault((Key["pk"], Key["sk"]), dict(Key))
        values = ExpressionAttributeValues or {}
        if ConditionExpression == "attribute_not_exists(run_id)":
            if "run_id" in row:
                raise error("ConditionalCheckFailedException")
        elif row.get("run_id") != values.get(":run"):
            raise error("ConditionalCheckFailedException")
        if "SET run_id" in UpdateExpression:
            row.update(run_id=values[":run"], pending_cutoff=values[":cutoff"], started_at=values[":started"])
        elif "SET execution_arn" in UpdateExpression:
            row["execution_arn"] = values[":arn"]
        elif "SET latest_quality" in UpdateExpression:
            row.update(latest_quality=values[":quality"], latest_quality_status=values[":status"],
                       quality_report_uri=values[":uri"])
        else:
            if "SET cutoff" in UpdateExpression:
                if row.get("pending_cutoff") != values[":cutoff"]:
                    raise error("ConditionalCheckFailedException")
                row["cutoff"] = values[":cutoff"]
        if "REMOVE " in UpdateExpression:
            for field in UpdateExpression.split("REMOVE ", 1)[1].split(", "):
                row.pop(field, None)


class Dynamo:
    def __init__(self, table):
        self.table = table
        self.transactions = 0
        self.lose_response = False

    def transact_write_items(self, TransactItems):
        serializer = TypeDeserializer()
        decode = lambda row: {k: serializer.deserialize(v) for k, v in row.items()}
        staged = copy.deepcopy(self.table.rows)
        try:
            for operation in TransactItems:
                if "Put" in operation:
                    args = operation["Put"]
                    item = decode(args["Item"])
                    if args.get("ConditionExpression") and (item["pk"], item["sk"]) in self.table.rows:
                        raise error("TransactionCanceledException")
                    self.table.put_item(item)
                else:
                    args = dict(operation["Update"])
                    args.pop("TableName")
                    args["Key"] = decode(args["Key"])
                    args["ExpressionAttributeValues"] = decode(args["ExpressionAttributeValues"])
                    self.table.update_item(**args)
        except Exception:
            self.table.rows = staged
            raise
        self.transactions += 1
        if self.lose_response:
            self.lose_response = False
            raise error("InternalServerError")


class S3:
    def __init__(self):
        self.objects = {"raw/dataset.json": json.dumps(dataset()).encode()}
        self.copies = 0

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            raise error("404")
        return {}

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise error("NoSuchKey")
        return {"Body": io.BytesIO(self.objects[Key])}

    def put_object(self, Bucket, Key, Body, **kwargs):
        if Key in self.objects:
            raise error("PreconditionFailed")
        self.objects[Key] = Body
        if "/source/" in Key:
            self.copies += 1


class SageMaker:
    def __init__(self):
        self.starts = []
        self.status = "Executing"
        self.lose_response = False

    def start_pipeline_execution(self, **kwargs):
        self.starts.append(kwargs)
        if self.lose_response:
            self.lose_response = False
            raise error("InternalServerError")
        return {"PipelineExecutionArn": "arn:test:execution/" + kwargs["ClientRequestToken"]}

    def describe_pipeline_execution(self, **kwargs):
        return {"PipelineExecutionStatus": self.status}


@unittest.skipUnless(HAS_AWS, "Install the aws extra for boto3 serialization checks")
class ReplayAndPublicationTests(unittest.TestCase):
    def setUp(self):
        self.table, self.s3, self.sm = Table(), S3(), SageMaker()
        self.dynamo = Dynamo(self.table)
        self.env = patch.dict(os.environ, {"RESULTS_TABLE": "test-results", "DATA_BUCKET": "test-data",
                                          "PIPELINE_NAME": "retail-forecast", "INITIAL_CUTOFF": "196"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.resource = patch("boto3.resource", return_value=argparse.Namespace(Table=lambda name: self.table))
        self.client = patch("boto3.client", side_effect=lambda service: {
            "s3": self.s3, "sagemaker": self.sm, "dynamodb": self.dynamo}[service])
        self.resource.start()
        self.client.start()
        self.addCleanup(self.resource.stop)
        self.addCleanup(self.client.stop)

    def pending(self):
        return self.table.rows[("REPLAY", "STATE")]

    def test_invalid_json_is_quarantined_without_start_or_advancing_active_publication(self):
        self.table.rows[("REPLAY", "STATE")] = {"pk": "REPLAY", "sk": "STATE", "cutoff": 196}
        active = {"pk": "CATALOG", "sk": "META", "payload": {"private_test": "active publication"}}
        self.table.rows[("CATALOG", "META")] = copy.deepcopy(active)
        self.s3.objects["raw/dataset.json"] = b"invalid private observations"
        result = ingest.handler({}, None)
        self.assertEqual(result["status"], "quarantined")
        self.assertEqual(self.sm.starts, [])
        self.assertEqual(self.pending()["cutoff"], 196)
        self.assertNotIn("run_id", self.pending())
        self.assertEqual(self.table.rows[("CATALOG", "META")], active)
        self.assertEqual(self.pending()["latest_quality_status"], "quarantined")
        self.assertFalse(self.pending()["latest_quality"]["passed"])
        report_key = result["quality_report_uri"].split("test-data/", 1)[1]
        report = json.loads(self.s3.objects[report_key])
        self.assertEqual(report["issues"][0]["code"], "snapshot_parse")
        self.assertNotIn("private observations", json.dumps(report))

    def test_missing_product_blocks_and_corrected_snapshot_retries_same_replay_day(self):
        from retail_forecast.storage import build_catalog
        accepted = dataset()
        additional = copy.deepcopy(accepted["series"][0])
        additional["item_id"] = "FOODS_1_002"
        accepted["series"].append(additional)
        catalog = build_catalog(accepted, "aws", ["seasonal"])
        self.table.rows[("CATALOG", "META")] = {"pk": "CATALOG", "sk": "META", "payload": catalog}
        rejected = ingest.handler({}, None)
        self.assertEqual(rejected["status"], "quarantined")
        self.assertEqual(self.sm.starts, [])
        self.assertIn("expected_snapshot", [issue["code"] for issue in self.pending()["latest_quality"]["issues"]])
        self.s3.objects["raw/dataset.json"] = json.dumps(accepted).encode()
        retried = ingest.handler({}, None)
        self.assertEqual(retried["status"], "started")
        self.assertEqual(retried["cutoff"], rejected["cutoff"])
        self.assertTrue(self.pending()["latest_quality"]["passed"])
        self.assertEqual(len(self.sm.starts), 1)

    def test_missing_source_object_is_quarantined_and_can_be_retried(self):
        self.s3.objects.pop("raw/dataset.json")
        rejected = ingest.handler({}, None)
        self.assertEqual(rejected["status"], "quarantined")
        self.assertEqual(self.sm.starts, [])
        self.assertNotIn("run_id", self.pending())
        self.s3.objects["raw/dataset.json"] = json.dumps(dataset()).encode()
        self.assertEqual(ingest.handler({}, None)["cutoff"], rejected["cutoff"])

    def test_oversized_s3_source_is_rejected_without_reading_or_freezing_its_body(self):
        from retail_forecast.quality import MAX_SNAPSHOT_BYTES
        body = Mock()
        body.read.side_effect = AssertionError("Do not allocate an oversized source body")
        with patch.object(self.s3, "get_object", return_value={"Body": body, "ContentLength": MAX_SNAPSHOT_BYTES + 1}):
            result = ingest.handler({}, None)
        self.assertEqual(result["status"], "quarantined")
        self.assertEqual(self.sm.starts, [])
        body.read.assert_not_called()
        body.close.assert_called_once()
        self.assertEqual(self.s3.copies, 0)
        self.assertEqual(self.pending()["latest_quality"]["issues"][0]["code"], "snapshot_budget")

    def test_quarantine_cannot_clear_a_newer_run_lock(self):
        self.s3.objects["raw/dataset.json"] = b"malformed snapshot"
        original_put = self.s3.put_object
        def concurrent_put(**kwargs):
            original_put(**kwargs)
            if "/quality/" in kwargs["Key"]:
                self.pending().update(run_id="newer-run", pending_cutoff=197)
        with patch.object(self.s3, "put_object", side_effect=concurrent_put):
            result = ingest.handler({}, None)
        self.assertEqual(result["status"], "concurrent_update")
        self.assertEqual(self.pending()["run_id"], "newer-run")
        self.assertEqual(self.pending()["pending_cutoff"], 197)
        self.assertNotIn("latest_quality", self.pending())
        self.assertEqual(self.sm.starts, [])

    def test_lost_start_response_reuses_token_and_frozen_snapshot(self):
        self.sm.lose_response = True
        with self.assertRaises(ClientError):
            ingest.handler({}, None)
        first_state = dict(self.pending())
        frozen = dict(self.s3.objects)
        self.s3.objects["raw/dataset.json"] = b"changed-raw-object"
        retry = ingest.handler({}, None)
        self.assertEqual(retry["status"], "started")
        self.assertEqual(self.sm.starts[0], self.sm.starts[1])
        self.assertEqual(self.pending()["run_id"], first_state["run_id"])
        snapshot = next(k for k in frozen if k.startswith("runs/"))
        self.assertEqual(self.s3.objects[snapshot], frozen[snapshot])
        self.assertEqual(self.s3.copies, 1)
        self.assertNotIn("cutoff", self.pending())
        self.assertEqual(ingest.handler({}, None)["status"], "in_progress")
        self.assertEqual(len(self.sm.starts), 2)

    def test_failed_or_rejected_execution_retries_same_day_with_new_token(self):
        first = ingest.handler({}, None)
        run = self.pending()["run_id"]
        self.sm.status = "Failed"
        retry = ingest.handler({}, None)
        self.assertEqual(retry["cutoff"], first["cutoff"])
        self.assertNotEqual(self.pending()["run_id"], run)
        self.assertNotIn("cutoff", self.pending())

    def inputs(self, root, include_forecast=True, passed=True):
        from retail_forecast.storage import build_catalog
        catalog = build_catalog(dataset(), "aws", ["seasonal"])
        catalog["default_cutoff"] = 196
        report = {"passed": passed, "source": "synthetic", "model": "seasonal", "wape": 0,
                  "baseline_wape": 0, "coverage": 1, "reason": "Test report"}
        jobs.write_json(root / "catalog/catalog.json", catalog)
        jobs.write_json(root / "labels/labels.json", {"CA_1#FOODS_1_001": [10] * 28})
        jobs.write_json(root / "evaluation/evaluation.json", report)
        if include_forecast:
            path = root / "forecast/requests.jsonl.out"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"store_id": "CA_1", "item_id": "FOODS_1_001", "model": "seasonal",
                                       "cutoff": 196, "forecast": [{"p50": 10}] * 28}) + "\n")

    def args(self):
        return argparse.Namespace(run_id=self.pending()["run_id"], cutoff=196, package_arn="arn:test:package/1")

    def test_incomplete_batch_does_not_advance_cursor_or_catalog(self):
        ingest.handler({}, None)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.inputs(root, include_forecast=False)
            with patch.object(jobs, "INPUT", root), self.assertRaisesRegex(ValueError, "incomplete"):
                jobs.publish(self.args())
        self.assertNotIn("cutoff", self.pending())
        self.assertNotIn(("CATALOG", "META"), self.table.rows)
        self.assertEqual(self.dynamo.transactions, 0)

    def test_complete_batch_commits_atomically_and_retry_cannot_rewrite(self):
        ingest.handler({}, None)
        args = self.args()
        self.dynamo.lose_response = True
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.inputs(root)
            with patch.object(jobs, "INPUT", root):
                jobs.publish(args)
                self.assertEqual(self.pending()["cutoff"], 196)
                self.assertNotIn("run_id", self.pending())
                self.assertEqual(self.table.rows[("CATALOG", "META")]["payload"]["min_cutoff"], 196)
                published_catalog = self.table.rows[("CATALOG", "META")]["payload"]
                self.assertEqual(published_catalog["published_run_id"], args.run_id)
                self.assertEqual(published_catalog["snapshot_sha256"], self.pending()["latest_quality"]["snapshot_sha256"])
                self.assertIn("+00:00", published_catalog["published_at"])
                forecast = self.table.rows[("SERIES#CA_1#FOODS_1_001", "FORECAST#000000196#seasonal")]["payload"]
                self.assertEqual(forecast["_run_id"], args.run_id)
                self.assertEqual(len(forecast["_actuals"]), 28)
                writes = self.table.writes
                jobs.publish(args)
                self.assertEqual(self.table.writes, writes)
                args.run_id = "another-run"
                with self.assertRaisesRegex(ValueError, "another run"):
                    jobs.publish(args)
        self.assertEqual(self.dynamo.transactions, 1)
        self.assertEqual(ingest.handler({}, None)["cutoff"], 197)

    def test_stale_run_cannot_write_rows(self):
        ingest.handler({}, None)
        args = self.args()
        args.run_id = "stale-run"
        with self.assertRaisesRegex(ValueError, "does not own"):
            jobs.publish(args)
        self.assertEqual(self.table.writes, 0)

    def test_rejection_records_once_and_does_not_advance(self):
        ingest.handler({}, None)
        args = self.args()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.inputs(root, passed=False)
            with patch.object(jobs, "INPUT", root):
                with self.assertRaisesRegex(ValueError, "rejected"):
                    jobs.publish(args)
                jobs.reject(args)
                jobs.reject(args)
        releases = [v for (pk, sk), v in self.table.rows.items() if pk == "RELEASES"]
        self.assertEqual(len(releases), 1)
        self.assertEqual(releases[0]["payload"]["status"], "rejected")
        self.assertNotIn("cutoff", self.pending())

    def test_end_of_replay_does_not_start_job(self):
        self.table.rows[("REPLAY", "STATE")] = {"pk": "REPLAY", "sk": "STATE", "cutoff": 224}
        result = ingest.handler({}, None)
        self.assertEqual(result["status"], "replay_complete")
        self.assertEqual(result["last_cutoff"], 224)
        self.assertEqual(self.sm.starts, [])
        self.assertNotIn("run_id", self.pending())


if __name__ == "__main__":
    unittest.main()
