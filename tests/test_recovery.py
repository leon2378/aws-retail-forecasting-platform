"""Restore comparison and guarded export tests never contact AWS."""
import copy
from contextlib import redirect_stdout
import hashlib
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from aws import recovery
from aws.schema import MAX_ENTITY_BYTES, SECTIONS
from orderflow.engine import Engine, initial_state


ROOT = Path(__file__).resolve().parents[1]
ACCOUNT = "123456789012"
REGION = "ap-southeast-2"
TABLE = "orderflow-replacement-test"


class MemoryRepository:
    mode = "aws"

    def __init__(self):
        self.state = initial_state()

    def load(self):
        return copy.deepcopy(self.state)

    def transact(self, mutator):
        return mutator(self.state)


def complete_state():
    repository = MemoryRepository()
    engine = Engine(repository)
    finished, _ = engine.submit({"customer": "Private test customer",
                                "items": [{"sku": "TOTE-001", "quantity": 1}],
                                "idempotency_key": "finished-private-request"})
    engine.process_order(finished["id"], expected_stage="payment")
    engine.process_order(finished["id"], expected_stage="fulfillment")
    engine.submit({"customer": "Second private test customer",
                   "items": [{"sku": "MUG-004", "quantity": 2}],
                   "idempotency_key": "reserved-private-request"})
    repository.state["drills"]["test-drill"] = {"id": "test-drill", "status": "running",
                                               "checks": [], "steps": [], "passed": False,
                                               "at": "2026-10-10T01:00:00Z"}
    return repository.load()


def table_description():
    return {"TableName": TABLE, "TableStatus": "ACTIVE",
            "TableArn": f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/{TABLE}",
            "KeySchema": [{"AttributeName": "PK", "KeyType": "HASH"},
                          {"AttributeName": "SK", "KeyType": "RANGE"}],
            "AttributeDefinitions": [{"AttributeName": "PK", "AttributeType": "S"},
                                     {"AttributeName": "SK", "AttributeType": "S"}]}


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.state = complete_state()

    def test_snapshot_copies_all_sections_and_hashes_deterministic_json(self):
        snapshot = recovery.make_snapshot(self.state, 9)
        self.assertEqual(set(snapshot["state"]), set(SECTIONS))
        self.assertTrue(all(snapshot["state"][section] for section in SECTIONS))
        self.assertEqual(snapshot["state_sha256"], hashlib.sha256(recovery.canonical_json(self.state)).hexdigest())
        reversed_state = {section: dict(reversed(list(rows.items())))
                          for section, rows in reversed(list(self.state.items()))}
        self.assertEqual(snapshot["state_sha256"], recovery.make_snapshot(reversed_state, 10)["state_sha256"])
        self.state["inventory"]["TOTE-001"]["name"] = "Changed after export"
        self.assertNotEqual(snapshot["state"], self.state)

    def test_compare_ignores_revision_but_compares_all_entity_payloads(self):
        source = recovery.make_snapshot(self.state, 1)
        replacement = recovery.make_snapshot(self.state, 800)
        report = recovery.compare_snapshots(source, replacement)
        self.assertTrue(report["matches"])
        self.assertTrue(report["state_matches"])
        self.assertEqual(report["source_revision"], 1)
        self.assertEqual(report["replacement_revision"], 800)
        self.assertFalse(report["revision_matches"])
        self.assertEqual(report["differences"], {"missing": 0, "extra": 0, "changed": 0})
        self.assertEqual(set(report["sections"]), set(SECTIONS))

    def test_revision_requirement_needs_both_manifests_at_identical_revision(self):
        source = recovery.make_snapshot(self.state, 9)
        same = recovery.make_snapshot(self.state, 9)
        self.assertTrue(recovery.compare_snapshots(source, same, require_revision_match=True)["matches"])
        for replacement in (recovery.make_snapshot(self.state, 10), self.state):
            report = recovery.compare_snapshots(source, replacement, require_revision_match=True)
            self.assertTrue(report["state_matches"])
            self.assertFalse(report["matches"])
            self.assertTrue(report["revision_required"])
        raw_report = recovery.compare_snapshots(self.state, self.state, require_revision_match=True)
        self.assertIsNone(raw_report["source_revision"])
        self.assertIsNone(raw_report["replacement_revision"])
        self.assertIsNone(raw_report["revision_matches"])
        self.assertFalse(raw_report["matches"])

    def test_raw_canonical_state_is_supported_and_extra_sections_rejected(self):
        self.assertTrue(recovery.compare_snapshots(self.state, recovery.make_snapshot(self.state, 0))["matches"])
        self.assertTrue(recovery.validate_snapshot(self.state)["valid"])
        invalid = copy.deepcopy(self.state)
        invalid["unknown"] = {}
        with self.assertRaises(recovery.RecoveryError):
            recovery.validate_snapshot(invalid)

    def test_altered_entity_in_every_section_is_detected_without_identifiers(self):
        for section in SECTIONS:
            with self.subTest(section=section):
                replacement = copy.deepcopy(self.state)
                key = next(iter(replacement[section]))
                replacement[section][key]["restore_test_note"] = "Private changed value"
                report = recovery.compare_snapshots(self.state, replacement)
                self.assertFalse(report["matches"])
                self.assertEqual(report["sections"][section]["changed"], 1)
                rendered = json.dumps(report)
                self.assertNotIn(key, rendered)
                self.assertNotIn("Private", rendered)
                self.assertNotIn("customer", rendered)

    def test_missing_and_extra_entities_are_reported(self):
        replacement = copy.deepcopy(self.state)
        replacement["inventory"].pop("LAMP-002")
        replacement["outbox"].pop(next(iter(replacement["outbox"])))
        replacement["drills"]["extra-private-drill"] = {
            **replacement["drills"]["test-drill"], "id": "extra-private-drill"}
        report = recovery.compare_snapshots(self.state, replacement)
        self.assertFalse(report["matches"])
        self.assertEqual(report["differences"], {"missing": 2, "extra": 1, "changed": 0})

    def test_json_numeric_type_changes_are_not_lost_to_python_equality(self):
        source = initial_state()
        source["inventory"]["TOTE-001"]["annotation"] = 1
        replacement = copy.deepcopy(source)
        replacement["inventory"]["TOTE-001"]["annotation"] = True
        self.assertEqual(source, replacement)
        report = recovery.compare_snapshots(source, replacement)
        self.assertEqual(report["sections"]["inventory"]["changed"], 1)
        self.assertFalse(report["matches"])

    def test_reservation_and_missing_effect_idempotency_corruption_rejected(self):
        corruptions = []
        reserved = copy.deepcopy(self.state)
        reserved["inventory"]["MUG-004"]["reserved"] = 0
        corruptions.append(reserved)
        for section in ("effects", "idempotency", "work"):
            invalid = copy.deepcopy(self.state)
            invalid[section].pop(next(iter(invalid[section])))
            corruptions.append(invalid)
        for invalid in corruptions:
            with self.subTest(section=invalid):
                with self.assertRaisesRegex(recovery.RecoveryError, "invariants"):
                    recovery.compare_snapshots(self.state, invalid)

    def test_invalid_entity_key_and_capacity_are_rejected(self):
        invalid = initial_state()
        invalid["inventory"][""] = {**invalid["inventory"]["TOTE-001"], "sku": ""}
        with self.assertRaises(recovery.RecoveryError):
            recovery.make_snapshot(invalid, 0)
        invalid = initial_state()
        invalid["inventory"]["TOTE-001"]["large"] = "x" * MAX_ENTITY_BYTES
        with self.assertRaises(recovery.RecoveryError):
            recovery.make_snapshot(invalid, 0)

    def test_manifest_validation_rejects_tampering_and_unsupported_metadata(self):
        original = recovery.make_snapshot(self.state, 0)
        invalid_manifests = []
        altered = copy.deepcopy(original)
        altered["state"]["inventory"]["TOTE-001"]["name"] = "Altered"
        invalid_manifests.append(altered)
        for field, invalid_values in (("schema", ["other", 1]), ("version", [2, True, "1"]),
                                      ("revision", [-1, True, 1.5, None]),
                                      ("state_sha256", ["invalid", "A" * 64, "0" * 64])):
            for value in invalid_values:
                invalid = copy.deepcopy(original)
                invalid[field] = value
                invalid_manifests.append(invalid)
        extra = copy.deepcopy(original)
        extra["account"] = ACCOUNT
        invalid_manifests.append(extra)
        missing = copy.deepcopy(original)
        missing.pop("revision")
        invalid_manifests.append(missing)
        for invalid in invalid_manifests:
            with self.assertRaises(recovery.RecoveryError):
                recovery.validate_snapshot(invalid)

    def test_public_repository_stable_snapshot_is_used_without_load_or_mutation(self):
        repository = Mock(spec=["snapshot"])
        repository.snapshot.return_value = (self.state, 42)
        snapshot = recovery.export_snapshot(repository)
        self.assertEqual(snapshot["revision"], 42)
        repository.snapshot.assert_called_once_with()
        self.assertEqual(repository.mock_calls, [unittest.mock.call.snapshot()])


class SnapshotFileTests(unittest.TestCase):
    def setUp(self):
        scratch = ROOT / "build"
        scratch.mkdir(exist_ok=True)
        self.temporary = TemporaryDirectory(dir=scratch)
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.snapshot = recovery.make_snapshot(initial_state(), 0)

    def invoke(self, *arguments):
        output = StringIO()
        with redirect_stdout(output):
            status = recovery.main(list(map(str, arguments)))
        return status, json.loads(output.getvalue())

    def test_exclusive_export_round_trip_and_existing_destination_preserved(self):
        path = self.folder / "private" / "snapshot.json"
        recovery.write_snapshot(self.snapshot, path)
        before = path.read_bytes()
        self.assertEqual(recovery.load_snapshot(path), self.snapshot)
        with self.assertRaisesRegex(recovery.RecoveryError, "already exists"):
            recovery.write_snapshot(self.snapshot, path)
        self.assertEqual(path.read_bytes(), before)
        if os.name != "nt":
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_duplicate_keys_nonfinite_deep_and_malformed_json_rejected(self):
        path = self.folder / "invalid.json"
        malformed = ('{"inventory":{},"inventory":{}}',
                     '{"outer":{"x":1,"x":2}}', '{"x":NaN}', '{"x":Infinity}',
                     '{"x":-Infinity}', '{"x":1e999}', "not JSON",
                     '[' * 2000 + '0' + ']' * 2000)
        for value in malformed:
            with self.subTest(value=value[:50]):
                path.write_text(value, encoding="utf-8")
                status, result = self.invoke("validate", path)
                self.assertEqual(status, 2)
                self.assertEqual(result["code"], "recovery_error")

    def test_in_memory_non_json_values_and_excessive_depth_rejected(self):
        for invalid in (float("nan"), float("inf"), {1: "number key"}, (1,), object()):
            with self.assertRaises(recovery.RecoveryError):
                recovery.canonical_json(invalid)
        nested = []
        root = nested
        for _ in range(recovery.MAX_JSON_DEPTH + 1):
            nested.append([])
            nested = nested[0]
        with self.assertRaises(recovery.RecoveryError):
            recovery.canonical_json(root)

    def test_bounded_input_and_output_files(self):
        path = self.folder / "large.json"
        path.write_bytes(b" " * (recovery.MAX_INPUT_BYTES + 1))
        with self.assertRaisesRegex(recovery.RecoveryError, "size limit"):
            recovery.load_snapshot(path)
        with patch.object(recovery, "MAX_INPUT_BYTES", 50):
            with self.assertRaises(recovery.RecoveryError):
                recovery.write_snapshot(self.snapshot, self.folder / "small.json")
        self.assertFalse((self.folder / "small.json").exists())

    def test_invalid_utf8_and_missing_files_are_structured_errors(self):
        path = self.folder / "invalid.json"
        path.write_bytes(b"\xff")
        for candidate in (path, self.folder / "missing.json"):
            status, result = self.invoke("validate", candidate)
            self.assertEqual(status, 2)
            self.assertNotIn(str(candidate), json.dumps(result))

    def test_offline_cli_exit_codes_and_revision_insensitivity(self):
        source = self.folder / "source.json"
        replacement = self.folder / "replacement.json"
        recovery.write_snapshot(self.snapshot, source)
        recovery.write_snapshot(recovery.make_snapshot(initial_state(), 99), replacement)
        self.assertEqual(self.invoke("validate", source)[0], 0)
        self.assertEqual(self.invoke("compare", source, replacement)[0], 0)
        status, result = self.invoke("compare", source, replacement, "--require-revision-match")
        self.assertEqual(status, 1)
        self.assertTrue(result["state_matches"])
        self.assertFalse(result["revision_matches"])
        matching = self.folder / "matching-revision.json"
        recovery.write_snapshot(self.snapshot, matching)
        self.assertEqual(self.invoke("compare", source, matching, "--require-revision-match")[0], 0)
        different = initial_state()
        different["inventory"]["TOTE-001"]["name"] = "Changed"
        replacement.write_bytes(recovery.canonical_json(different))
        status, result = self.invoke("compare", source, replacement)
        self.assertEqual(status, 1)
        self.assertFalse(result["matches"])

    def test_cli_revision_requirement_rejects_raw_state_even_when_contents_match(self):
        source = self.folder / "source.json"
        raw = self.folder / "raw.json"
        recovery.write_snapshot(self.snapshot, source)
        raw.write_bytes(recovery.canonical_json(self.snapshot["state"]))
        status, result = self.invoke("compare", source, raw, "--require-revision-match")
        self.assertEqual(status, 1)
        self.assertTrue(result["state_matches"])
        self.assertIsNone(result["replacement_revision"])
        self.assertIsNone(result["revision_matches"])

    def test_offline_commands_run_with_site_packages_disabled_and_no_sdk_import(self):
        path = self.folder / "source.json"
        recovery.write_snapshot(self.snapshot, path)
        for command in (("validate", str(path)), ("compare", str(path), str(path))):
            process = subprocess.run([sys.executable, "-S", "-m", "aws.recovery", *command],
                                     cwd=ROOT, capture_output=True, text=True, timeout=20)
            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertTrue(json.loads(process.stdout).get("valid", True))
        process = subprocess.run([sys.executable, "-S", "-c",
                                  "import sys; import aws.recovery; "
                                  "assert 'boto3' not in sys.modules; assert 'botocore' not in sys.modules"],
                                 cwd=ROOT, capture_output=True, text=True, timeout=20)
        self.assertEqual(process.returncode, 0, process.stderr)

    def test_existing_output_is_refused_before_any_aws_export(self):
        path = self.folder / "existing.json"
        path.write_text("preserved")
        with patch.object(recovery, "export_table_snapshot") as export:
            status, result = self.invoke("export", "--table", TABLE, "--region", REGION,
                                         "--profile", "test", "--expected-account", ACCOUNT,
                                         "--output", path)
        self.assertEqual(status, 2)
        self.assertIn("already exists", result["error"])
        export.assert_not_called()
        self.assertEqual(path.read_text(), "preserved")


class ExportGuardTests(unittest.TestCase):
    def setUp(self):
        self.sts = Mock(spec=["get_caller_identity"])
        self.sts.get_caller_identity.return_value = {"Account": ACCOUNT}
        self.dynamo = Mock(spec=["describe_table"])
        self.dynamo.describe_table.return_value = {"Table": table_description()}
        self.session = Mock(spec=["client"])
        self.session.client.side_effect = lambda service, **kwargs: {"sts": self.sts, "dynamodb": self.dynamo}[service]
        self.factory = Mock(return_value=self.session)
        self.repository = Mock(spec=["snapshot"])
        self.repository.snapshot.return_value = (complete_state(), 7)
        self.repository_factory = Mock(return_value=self.repository)

    def export(self, **overrides):
        arguments = {"table": TABLE, "region": REGION, "profile": "explicit-test-profile",
                     "expected_account": ACCOUNT, "session_factory": self.factory,
                     "repository_factory": self.repository_factory}
        arguments.update(overrides)
        return recovery.export_table_snapshot(**arguments)

    def test_guarded_export_is_read_only_and_identity_checks_precede_snapshot(self):
        snapshot = self.export()
        self.assertEqual(snapshot["revision"], 7)
        self.factory.assert_called_once_with(profile_name="explicit-test-profile", region_name=REGION)
        self.assertEqual([call.args[0] for call in self.session.client.call_args_list], ["sts", "dynamodb"])
        self.sts.get_caller_identity.assert_called_once_with()
        self.dynamo.describe_table.assert_called_once_with(TableName=TABLE)
        self.repository_factory.assert_called_once_with(TABLE, client=self.dynamo)
        self.repository.snapshot.assert_called_once_with()
        self.assertEqual(set(snapshot), {"schema", "version", "revision", "state_sha256", "state"})

    def test_wrong_account_stops_before_any_dynamodb_client(self):
        self.sts.get_caller_identity.return_value = {"Account": "999999999999"}
        with self.assertRaisesRegex(recovery.RecoveryError, "account"):
            self.export()
        self.assertEqual([call.args[0] for call in self.session.client.call_args_list], ["sts"])
        self.dynamo.describe_table.assert_not_called()
        self.repository_factory.assert_not_called()

    def test_identity_failure_stops_before_any_dynamodb_client_and_is_redacted(self):
        self.sts.get_caller_identity.side_effect = RuntimeError("Private account/profile/credential details")
        with self.assertRaises(recovery.RecoveryError) as failure:
            self.export()
        self.assertNotIn("Private", str(failure.exception))
        self.assertEqual([call.args[0] for call in self.session.client.call_args_list], ["sts"])

    def test_explicit_identity_arguments_validated_before_session_creation(self):
        cases = ({"table": ""}, {"table": "arn:aws:dynamodb:region:account:table/name"},
                 {"region": ""}, {"profile": ""}, {"profile": " test "},
                 {"expected_account": ""}, {"expected_account": "123"})
        for override in cases:
            with self.subTest(override=override):
                with self.assertRaises(recovery.RecoveryError):
                    self.export(**override)
        self.factory.assert_not_called()

    def test_inactive_mismatched_arn_and_bad_key_schema_stop_before_repository_read(self):
        cases = ({"TableStatus": "CREATING"}, {"TableName": "other-table"},
                 {"TableArn": f"arn:aws:dynamodb:us-east-1:{ACCOUNT}:table/{TABLE}"},
                 {"TableArn": f"arn:aws:dynamodb:{REGION}:999999999999:table/{TABLE}"},
                 {"TableArn": f"arn:aws:sqs:{REGION}:{ACCOUNT}:table/{TABLE}"},
                 {"TableArn": f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/other-table"},
                 {"KeySchema": [{"AttributeName": "PK", "KeyType": "HASH"}]},
                 {"KeySchema": [{"AttributeName": "PK", "KeyType": "HASH"},
                                {"AttributeName": "other", "KeyType": "RANGE"}]},
                 {"AttributeDefinitions": [{"AttributeName": "PK", "AttributeType": "N"},
                                           {"AttributeName": "SK", "AttributeType": "S"}]})
        for override in cases:
            with self.subTest(override=override):
                self.dynamo.describe_table.return_value = {"Table": {**table_description(), **override}}
                with self.assertRaises(recovery.RecoveryError):
                    self.export()
        self.repository_factory.assert_not_called()
        self.repository.snapshot.assert_not_called()

    def test_export_failures_never_return_partial_or_unverified_snapshot(self):
        self.repository.snapshot.side_effect = RuntimeError("Concurrent writes prevented stable snapshot; private id")
        with self.assertRaises(recovery.RecoveryError) as failure:
            self.export()
        self.assertNotIn("private", str(failure.exception))
        self.assertEqual(self.dynamo.mock_calls, [unittest.mock.call.describe_table(TableName=TABLE)])

    def test_actual_repository_paginated_export_retries_for_stable_revision(self):
        try:
            from aws.repository import DynamoRepository, encode
            from aws.schema import META_KEY, entity_items
        except ImportError:
            self.skipTest("Optional AWS SDK is absent; credential-free tests still run.")

        class ReadClient:
            def __init__(self, state):
                self.rows = entity_items(state)
                self.revision = 3
                self.calls = []
                self.interrupt = True

            def describe_table(self, **kwargs):
                self.calls.append("describe_table")
                return {"Table": table_description()}

            def get_item(self, **kwargs):
                self.calls.append("get_item")
                self.assert_consistent(kwargs)
                return {"Item": encode({**META_KEY, "schema": 1, "revision": self.revision})}

            @staticmethod
            def assert_consistent(kwargs):
                assert kwargs["ConsistentRead"] is True

            def query(self, **kwargs):
                self.calls.append("query")
                self.assert_consistent(kwargs)
                keys = sorted(self.rows)
                after = kwargs.get("ExclusiveStartKey", {}).get("SK", {}).get("S")
                keys = [key for key in keys if after is None or key > after]
                page = keys[:2]
                response = {"Items": [encode(self.rows[key]) for key in page]}
                if len(keys) > len(page):
                    response["LastEvaluatedKey"] = encode({"PK": "WORKSPACE", "SK": page[-1]})
                if self.interrupt:
                    self.revision += 1
                    self.interrupt = False
                return response

        client = ReadClient(complete_state())
        self.dynamo = client
        snapshot = self.export(repository_factory=lambda table, client: DynamoRepository(table, client, sleep=lambda _: None))
        self.assertEqual(snapshot["revision"], 4)
        self.assertTrue(recovery.validate_snapshot(snapshot)["valid"])
        self.assertGreater(client.calls.count("query"), 2)
        self.assertEqual(client.calls.count("get_item"), 4)
        self.assertEqual(set(client.calls), {"describe_table", "get_item", "query"})


if __name__ == "__main__":
    unittest.main()
