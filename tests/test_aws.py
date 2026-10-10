"""AWS boundary tests use in-memory service doubles; no cloud credentials needed."""
import copy
import hashlib
import base64
import json
import os
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from botocore.exceptions import ClientError

from aws.handlers import api, drill, outbox, worker
from aws.package import package
from aws.repository import DynamoRepository, META_KEY, RepositoryUnavailable, decode, encode
from orderflow.auth import Principal
from orderflow.engine import Engine, initial_state, validate_state


def service_error(code, reasons=None):
    details = {"Error": {"Code": code, "Message": "Test service failure"}}
    if reasons is not None:
        details["CancellationReasons"] = [{"Code": reason} for reason in reasons]
    return ClientError(details, "TransactWriteItems")


class DynamoDouble:
    def __init__(self, page_size=2):
        self.rows, self.tokens, self.calls = {}, {}, []
        self.page_size, self.queries, self.conflict, self.lose_response = page_size, [], False, False
        self.after_first_page = None

    def get_item(self, TableName, Key, ConsistentRead):
        assert ConsistentRead
        key = decode(Key)
        row = self.rows.get(key["SK"])
        return {"Item": encode(copy.deepcopy(row))} if row else {}

    def query(self, **kwargs):
        assert kwargs["ConsistentRead"]
        self.queries.append(kwargs)
        keys = sorted(self.rows)
        after = decode(kwargs["ExclusiveStartKey"])["SK"] if "ExclusiveStartKey" in kwargs else None
        remaining = [key for key in keys if after is None or key > after]
        page = remaining[:self.page_size]
        result = {"Items": [encode(copy.deepcopy(self.rows[key])) for key in page]}
        if len(remaining) > self.page_size:
            result["LastEvaluatedKey"] = encode({"PK": "WORKSPACE", "SK": page[-1]})
        if self.after_first_page and after is None:
            callback, self.after_first_page = self.after_first_page, None
            callback(self)
        return result

    def transact_write_items(self, TransactItems, ClientRequestToken):
        assert len(TransactItems) <= 100
        self.calls.append((copy.deepcopy(TransactItems), ClientRequestToken))
        if ClientRequestToken in self.tokens:
            assert self.tokens[ClientRequestToken] == TransactItems
            return {}
        if self.conflict:
            self.conflict = False
            self.rows["META"]["revision"] += 1
            self.rows["inventory#TOTE-001"]["payload"]["on_hand"] += 2
            raise service_error("TransactionCanceledException", ["ConditionalCheckFailed"])
        staged = copy.deepcopy(self.rows)
        for operation in TransactItems:
            if "Put" in operation:
                arguments = operation["Put"]
                item = decode(arguments["Item"])
                if arguments.get("ConditionExpression") and item["SK"] in staged:
                    raise service_error("TransactionCanceledException", ["ConditionalCheckFailed"])
                staged[item["SK"]] = item
            elif "Delete" in operation:
                staged.pop(decode(operation["Delete"]["Key"])["SK"], None)
            else:
                values = decode(operation["Update"]["ExpressionAttributeValues"])
                if staged["META"]["revision"] != values[":expected"]:
                    raise service_error("TransactionCanceledException", ["ConditionalCheckFailed"])
                staged["META"]["revision"] = values[":next"]
        self.rows, self.tokens[ClientRequestToken] = staged, copy.deepcopy(TransactItems)
        if self.lose_response:
            self.lose_response = False
            raise service_error("InternalServerError")
        return {}


def repository():
    client = DynamoDouble()
    store = DynamoRepository("orderflow-test", client, sleep=lambda _: None)
    store.initialize(initial_state())
    return store, client


def payload(key="aws-test-request", scenario="happy_path"):
    return {"customer": "Test store", "items": [{"sku": "TOTE-001", "quantity": 1}],
            "idempotency_key": key, "scenario": scenario}


class RepositoryTests(unittest.TestCase):
    def test_bootstrap_explicit_conditional_and_no_overwrite(self):
        client = DynamoDouble()
        store = DynamoRepository("table", client, sleep=lambda _: None)
        with self.assertRaisesRegex(RepositoryUnavailable, "not initialized"):
            store.load()
        self.assertEqual(client.calls, [])
        self.assertTrue(store.initialize(initial_state()))
        engine = Engine(store)
        engine.restock("TOTE-001", 2)
        self.assertFalse(store.initialize(initial_state()))
        self.assertEqual(store.load()["inventory"]["TOTE-001"]["on_hand"], 50)
        self.assertNotIn("payload", client.rows["META"])

    def test_paginated_consistent_snapshot_restarts_if_revision_changes(self):
        store, client = repository()
        def concurrent_change(service):
            service.rows["inventory#TOTE-001"]["payload"]["on_hand"] = 99
            service.rows["META"]["revision"] += 1
        client.after_first_page = concurrent_change
        state = store.load()
        self.assertEqual(state["inventory"]["TOTE-001"]["on_hand"], 99)
        self.assertGreater(len(client.queries), 4)
        self.assertTrue(any("ExclusiveStartKey" in call for call in client.queries))

    def test_atomic_order_entities_inventory_and_outbox_and_idempotency(self):
        store, client = repository()
        engine = Engine(store)
        order, replayed = engine.submit(payload(), "operator")
        self.assertFalse(replayed)
        first_transaction = client.calls[-1][0]
        keys = [decode(operation["Put"]["Item"])["SK"] for operation in first_transaction if "Put" in operation]
        for section in ("orders", "work", "idempotency", "events", "outbox", "inventory"):
            self.assertTrue(any(key.startswith(section + "#") for key in keys), section)
        self.assertEqual(first_transaction[0]["Update"]["ConditionExpression"], "revision = :expected")
        calls = len(client.calls)
        repeated, replayed = engine.submit(payload(), "operator")
        self.assertTrue(replayed)
        self.assertEqual(repeated["id"], order["id"])
        self.assertEqual(len(client.calls), calls)
        validate_state(store.load())

    def test_revision_conflict_recomputes_against_new_stock(self):
        store, client = repository()
        client.conflict = True
        Engine(store).restock("TOTE-001", 3)
        self.assertEqual(store.load()["inventory"]["TOTE-001"]["on_hand"], 53)
        self.assertEqual(client.rows["META"]["revision"], 2)

    def test_lost_commit_response_reuses_token_without_double_restock(self):
        store, client = repository()
        client.lose_response = True
        Engine(store).restock("TOTE-001", 3)
        self.assertEqual(store.load()["inventory"]["TOTE-001"]["on_hand"], 51)
        self.assertEqual(client.calls[-1], client.calls[-2])

    def test_transaction_limits_reject_before_service_write(self):
        store, client = repository()
        calls = len(client.calls)
        def oversized(state):
            state["events"].update({str(index): {"id": str(index)} for index in range(100)})
        with self.assertRaisesRegex(RepositoryUnavailable, "100-item"):
            store.transact(oversized)
        self.assertEqual(len(client.calls), calls)
        self.assertEqual(store.load()["events"], {})
        with self.assertRaisesRegex(RepositoryUnavailable, "item size"):
            store.transact(lambda state: state["events"].update({"big": {"body": "x" * 320001}}))
        self.assertEqual(len(client.calls), calls)

    def test_permission_and_validation_failures_are_not_retried_as_conflicts(self):
        store, client = repository()
        for error in (service_error("AccessDeniedException"), service_error("TransactionCanceledException", ["ValidationError"])):
            with patch.object(client, "transact_write_items", side_effect=error) as write:
                with self.assertRaises(ClientError):
                    Engine(store).restock("TOTE-001", 1)
                self.assertEqual(write.call_count, 1)

    def test_admission_limit_is_atomic_and_does_not_block_existing_order(self):
        store, _ = repository()
        engine = Engine(store)
        with patch("aws.repository.MAX_ACCEPTED_ORDERS", 1):
            order, _ = engine.submit(payload("first-accepted-key"))
            with self.assertRaisesRegex(RepositoryUnavailable, "capacity reached"):
                engine.submit(payload("second-rejected-key"))
            self.assertEqual(store.load()["inventory"]["TOTE-001"]["reserved"], 1)
            repeated, replayed = engine.submit(payload("first-accepted-key"))
            self.assertTrue(replayed)
            self.assertEqual(order["id"], repeated["id"])
            engine.process_order(order["id"], expected_stage="payment")
            engine.process_order(order["id"], expected_stage="fulfillment")
            self.assertEqual(engine.get_order(order["id"])["status"], "completed")

    def test_admission_reserves_future_recovery_space(self):
        store, client = repository()
        with patch("aws.repository.MAX_STATE_BYTES", 1_000_000):
            with self.assertRaisesRegex(RepositoryUnavailable, "headroom"):
                Engine(store).submit(payload())
        self.assertEqual(store.load()["orders"], {})
        self.assertEqual(len(client.calls), 1)


class QueueTests(unittest.TestCase):
    def test_cancellation_during_publication_preserves_discard_and_stale_delivery_is_safe(self):
        for paid in (False, True):
            with self.subTest(paid=paid):
                store, _ = repository()
                engine = Engine(store)
                order, _ = engine.submit(payload())
                if paid:
                    payment_queue = Mock()
                    outbox.publish(store, payment_queue, "queue")
                    payment_message = payment_queue.send_message.call_args.kwargs["MessageBody"]
                    self.assertEqual(worker.process_records([{"messageId": "payment", "body": payment_message}], engine), {"batchItemFailures": []})
                pending = next(entry for entry in store.load()["outbox"].values() if entry["status"] == "pending")
                queue = Mock()
                # Cancel after the publisher's snapshot and send, before its
                # acknowledgement. The acknowledgement must not undo discard.
                queue.send_message.side_effect = lambda **kwargs: engine.cancel(order["id"])
                published = outbox.publish(store, queue, "queue")
                self.assertEqual(published["sent"], 1)
                message = queue.send_message.call_args.kwargs["MessageBody"]
                state = store.load()
                self.assertEqual(state["outbox"][pending["id"]]["status"], "discarded")
                self.assertEqual(state["inventory"]["TOTE-001"]["reserved"], 0)
                self.assertEqual(state["inventory"]["TOTE-001"]["on_hand"], 48)
                for number in range(2):
                    self.assertEqual(worker.process_records([{"messageId": str(number), "body": message}], engine), {"batchItemFailures": []})
                self.assertEqual(store.load(), state)
                self.assertEqual(sum(effect["type"] == "payment" for effect in state["effects"].values()), int(paid))
                self.assertEqual(sum(effect["type"] == "refund" for effect in state["effects"].values()), int(paid))
                self.assertFalse(any(effect["type"] == "shipment" for effect in state["effects"].values()))
                repair_queue = Mock()
                self.assertEqual(outbox.publish(store, repair_queue, "queue")["sent"], 0)
                repair_queue.send_message.assert_not_called()
                validate_state(state)

    def test_publication_failure_retains_work_then_duplicate_send_is_safe(self):
        store, _ = repository()
        engine = Engine(store)
        order, _ = engine.submit(payload())
        queue = Mock()
        queue.send_message.side_effect = RuntimeError("Delivery interrupted")
        with self.assertLogs("aws.handlers.outbox", level="ERROR"):
            result = outbox.publish(store, queue, "queue")
        self.assertEqual(len(result["failed"]), 1)
        self.assertTrue(all(row["status"] == "pending" for row in store.load()["outbox"].values()))
        queue.send_message.side_effect = None
        with patch.object(store, "transact", side_effect=RuntimeError("Lost acknowledgment")):
            with self.assertLogs("aws.handlers.outbox", level="ERROR"):
                outbox.publish(store, queue, "queue")
        outbox.publish(store, queue, "queue")
        message = queue.send_message.call_args.kwargs["MessageBody"]
        records = [{"messageId": str(index), "body": message} for index in range(2)]
        self.assertEqual(worker.process_records(records, engine), {"batchItemFailures": []})
        self.assertEqual(sum(effect["type"] == "payment" for effect in store.load()["effects"].values()), 1)
        self.assertEqual(engine.get_order(order["id"])["stage"], "fulfillment")

    def test_partial_retry_does_not_retry_success_and_failures_redrive(self):
        store, _ = repository()
        engine = Engine(store)
        normal, _ = engine.submit(payload("normal-order-key"))
        failing, _ = engine.submit(payload("failing-order-key", "fulfillment_dead_letter"))
        engine.process_order(failing["id"], expected_stage="payment")
        messages = [
            {"messageId": "good", "body": json.dumps({"order_id": normal["id"], "stage": "payment", "event_id": "event1"})},
            {"messageId": "bad", "body": json.dumps({"order_id": failing["id"], "stage": "fulfillment", "event_id": "event2"})},
        ]
        self.assertEqual(worker.process_records(messages, engine), {"batchItemFailures": [{"itemIdentifier": "bad"}]})
        for _ in range(4):
            self.assertEqual(worker.process_records([messages[1]], engine)["batchItemFailures"], [{"itemIdentifier": "bad"}])
        self.assertEqual(store.load()["work"][failing["id"]]["attempts"], 3)
        self.assertEqual(store.load()["work"][failing["id"]]["status"], "dead_letter")
        self.assertEqual(engine.get_order(normal["id"])["stage"], "fulfillment")

    def test_poison_message_returns_failure(self):
        with self.assertLogs("aws.handlers.worker", level="ERROR"):
            result = worker.process_records([{"messageId": "poison", "body": "not-json"}], Mock())
        self.assertEqual(result, {"batchItemFailures": [{"itemIdentifier": "poison"}]})

    def test_receipt_evidence_links_failed_stage_without_logging_customer_or_body(self):
        store, _ = repository()
        engine = Engine(store)
        order, _ = engine.submit(payload("receipt-evidence", "fulfillment_dead_letter"))
        engine.process_order(order["id"], expected_stage="payment")
        record = {"messageId": "delivery-001", "attributes": {"ApproximateReceiveCount": "3"},
                  "body": json.dumps({"order_id": order["id"], "stage": "fulfillment", "event_id": "event-001"})}
        with self.assertLogs("aws.handlers.worker", level="INFO") as evidence:
            self.assertEqual(worker.process_records([record], engine),
                             {"batchItemFailures": [{"itemIdentifier": "delivery-001"}]})
        receipt = json.loads(evidence.records[0].getMessage().split(" ", 1)[1])
        self.assertEqual(receipt["receive_count"], 3)
        self.assertEqual(receipt["outcome"], "retry")
        self.assertEqual(receipt["order_id"], order["id"])
        self.assertEqual(receipt["stage"], "fulfillment")
        self.assertNotIn("Test store", evidence.records[0].getMessage())
        self.assertNotIn("body", receipt)

    def test_stream_failure_identifier_is_sequence_number(self):
        store, _ = repository()
        Engine(store).submit(payload())
        entry = next(iter(store.load()["outbox"].values()))
        queue = Mock()
        queue.send_message.side_effect = RuntimeError("SQS unavailable")
        event = {"Records": [{"eventName": "INSERT", "dynamodb": {"SequenceNumber": "123456", "NewImage": encode({"PK": "WORKSPACE", "SK": "outbox#" + entry["id"], "payload": entry})}}]}
        with patch.dict(os.environ, {"ORDERFLOW_TABLE": "table", "WORK_QUEUE_URL": "queue"}), patch("aws.repository.DynamoRepository", return_value=store), patch("boto3.client", return_value=queue), self.assertLogs("aws.handlers.outbox", level="ERROR"):
            result = outbox.handler(event, None)
        self.assertEqual(result, {"batchItemFailures": [{"itemIdentifier": "123456"}]})


ENV = {"ORDERFLOW_TABLE": "table", "AUTH_ENABLED": "true", "AUTH_ISSUER": "https://cognito-idp.ap-southeast-2.amazonaws.com/ap-southeast-2_test",
       "AUTH_CLIENT_ID": "client", "AUTH_DOMAIN": "https://orderflow-test.auth.ap-southeast-2.amazoncognito.com",
       "AUTH_CALLBACK_URL": "https://app.example/", "AUTH_LOGOUT_URL": "https://app.example/",
       "AUTH_SCOPES": "openid profile orderflow/read orderflow/write", "DRILL_MACHINE_ARN": "arn:aws:states:ap-southeast-2:123456789012:stateMachine:orderflow-recovery"}


def request(path="/api/orders", method="GET", role="operator", body=None, scope="orderflow/read orderflow/write"):
    now = int(time.time())
    claims = {"token_use": "access", "iss": ENV["AUTH_ISSUER"], "client_id": ENV["AUTH_CLIENT_ID"],
              "sub": "test-user", "scope": scope, "cognito:groups": role, "iat": str(now), "exp": str(now + 900)}
    return {"rawPath": path, "requestContext": {"http": {"method": method}, "authorizer": {"jwt": {"claims": claims}}},
            "body": json.dumps(body) if body is not None else ""}


class HttpAndDrillTests(unittest.TestCase):
    def setUp(self):
        self.store, self.client = repository()
        self.environment = patch.dict(os.environ, ENV)
        self.boundary = patch.object(api, "DynamoRepository", return_value=self.store)
        self.environment.start()
        self.boundary.start()
        self.addCleanup(self.environment.stop)
        self.addCleanup(self.boundary.stop)

    def test_public_health_config_never_load_or_initialize_storage(self):
        for path in ("/api/health", "/api/auth/config"):
            with patch.object(api, "DynamoRepository", side_effect=AssertionError("Public routes must not access data")):
                result = api.handler({"rawPath": path, "requestContext": {"http": {"method": "GET"}}}, None)
                self.assertEqual(result["statusCode"], 200)
                self.assertEqual(result["headers"]["cache-control"], "no-store")

    def test_missing_claims_viewer_write_and_unassigned_identity_fail_before_data(self):
        events = [(request(), 401), (request(method="POST", role="viewer", body=payload()), 403),
                  (request(role="unknown"), 403), (request(method="POST", body=payload(), scope="orderflow/read"), 403)]
        events[0][0]["requestContext"].pop("authorizer")
        for event, expected in events:
            with patch.object(api, "DynamoRepository", side_effect=AssertionError("Denied requests must not touch storage")):
                result = api.handler(event, None)
                self.assertEqual(result["statusCode"], expected)
                self.assertIsInstance(json.loads(result["body"])["error"], str)

    def test_request_json_and_idempotency_header_consistent(self):
        event = request(method="POST", body=payload())
        event["headers"] = {"Idempotency-Key": payload()["idempotency_key"]}
        self.assertEqual(api.handler(event, None)["statusCode"], 201)
        self.assertEqual(api.handler(event, None)["statusCode"], 200)
        event["headers"]["Idempotency-Key"] = "conflicting-header-key"
        self.assertEqual(api.handler(event, None)["statusCode"], 400)
        nested = '{"nested":' + "[" * 2000 + "0" + "]" * 2000 + "}"
        for raw, status in (("[]", 400), ('{"quantity":NaN}', 400), (nested, 400), ("x" * 16385, 413)):
            invalid = request(method="POST")
            invalid["body"] = raw
            self.assertEqual(api.handler(invalid, None)["statusCode"], status)
        encoded = request(method="POST")
        encoded.update(body=base64.b64encode(b"x" * 16385).decode(), isBase64Encoded=True)
        self.assertEqual(api.handler(encoded, None)["statusCode"], 413)

    def test_async_drill_visible_while_running_and_only_evidence_persisted(self):
        step_functions = Mock()
        step_functions.start_execution.return_value = {"executionArn": "arn:test:execution"}
        with patch("boto3.client", return_value=step_functions):
            result = api.handler(request("/api/operations/drill", "POST", "admin", {}), None)
        self.assertEqual(result["statusCode"], 202)
        identifier = json.loads(result["body"])["drill_id"]
        pending = Engine(self.store).operations()["drills"][0]
        self.assertEqual(pending["id"], identifier)
        self.assertEqual(pending["status"], "running")
        before = self.store.load()
        with patch.object(drill, "DynamoRepository", return_value=self.store):
            outcome = drill.handler({"drill_id": identifier, "actor": "test-user"}, None)
        self.assertTrue(outcome["passed"])
        after = self.store.load()
        for section in before.keys() - {"drills"}:
            self.assertEqual(after[section], before[section])
        self.assertEqual(after["drills"][identifier]["execution"], "AWS Step Functions Standard")

    def test_workflow_failure_persists_failed_evidence_without_rerunning_drill(self):
        principal = Principal("test-user", "admin", "Admin", "cognito")
        client = Mock()
        client.start_execution.return_value = {"executionArn": "arn:test:execution"}
        identifier = json.loads(api.start_drill(self.store, principal, client, "arn:test:machine")["body"])["drill_id"]
        with patch.object(drill, "DynamoRepository", return_value=self.store), patch("orderflow.engine.Engine", side_effect=AssertionError("Do not rerun a failed workflow")):
            result = drill.handler({"drill_id": identifier, "actor": principal.subject, "failure": {"Error": "Lambda.Timeout"}}, None)
        self.assertFalse(result["passed"])
        self.assertEqual(self.store.load()["drills"][identifier]["status"], "failed")
        self.assertEqual(self.store.load()["orders"], {})

    def test_failed_workflow_start_returns_flat_error_and_preserves_failed_record(self):
        client = Mock()
        client.start_execution.side_effect = RuntimeError("Start rejected")
        with patch("boto3.client", return_value=client), self.assertLogs("aws.handlers.api", level="ERROR"):
            result = api.handler(request("/api/operations/drill", "POST", "admin", {}), None)
        self.assertEqual(result["statusCode"], 500)
        self.assertEqual(json.loads(result["body"])["code"], "internal_error")
        record = Engine(self.store).operations()["drills"][0]
        self.assertEqual(record["status"], "failed")
        self.assertFalse(record["passed"])


class PackageTests(unittest.TestCase):
    def test_delivery_archives_exclude_private_configuration_and_include_web_assets(self):
        temporary_parent = Path.cwd() / "build"
        temporary_parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=temporary_parent) as directory:
            root = Path(directory)
            for name in ("orderflow/engine.py", "aws/repository.py", "frontend/app.js", "infra/storage.tf", "infra/terraform.tfvars.example", "infra/terraform.tfvars", "infra/private.tfstate", "infra/.terraform/secrets", "data/private.json", "build/private.json", "pyproject.toml", "README.md"):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("test")
            lambda_path, source_path = package(root, root / "build/package")
            with zipfile.ZipFile(lambda_path) as archive:
                self.assertEqual(set(archive.namelist()), {"orderflow/engine.py", "aws/repository.py"})
            with zipfile.ZipFile(source_path) as archive:
                names = set(archive.namelist())
                self.assertIn("frontend/app.js", names)
                self.assertIn("infra/terraform.tfvars.example", names)
                self.assertFalse(any("private" in name or ".terraform/" in name for name in names))
            manifest = json.loads((root / "build/package/manifest.json").read_text())
            self.assertEqual(manifest["lambda.zip"]["sha256"], hashlib.sha256(lambda_path.read_bytes()).hexdigest())


if __name__ == "__main__":
    unittest.main()
