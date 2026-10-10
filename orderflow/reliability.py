"""Offline integration rehearsal using real adapters and a simulated queue.

This queue is a deterministic test double, not an implementation or guarantee of
SQS behavior. Only the publisher, worker, domain engine and SQLite transactions
are real. No AWS client, credentials, deployed resource or live database is used.
"""

from contextlib import contextmanager
import json
import logging
from pathlib import Path
from tempfile import TemporaryDirectory

from .engine import Engine, initial_state, validate_state
from .storage import SQLiteRepository


class SimulatedQueue:
    """Small virtual-time queue double with partial acknowledgement and redrive.

    In this model, a failed fifth receive reaches the modeled DLQ on the next
    receive after visibility expires. AWS polling, redrive timing, retention,
    duplicate delivery and visibility behavior must be verified separately.
    """

    queue_url = "simulation://orderflow-work"

    def __init__(self, visibility_timeout_seconds=180, max_receive_count=5):
        for value in (visibility_timeout_seconds, max_receive_count):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError("Queue policy values must be positive integers.")
        self.visibility_timeout_seconds = visibility_timeout_seconds
        self.max_receive_count = max_receive_count
        self.now = 0
        self.messages = {}
        self.dead_letters = []
        self.fail_next_send = False
        self.lose_next_send_acknowledgement = False
        self._sequence = 0

    def send_message(self, *, QueueUrl, MessageBody):
        if QueueUrl != self.queue_url or not isinstance(MessageBody, str):
            raise ValueError("Use the simulation queue URL and a string body.")
        if self.fail_next_send:
            self.fail_next_send = False
            raise ConnectionError("Injected send failure before queue acceptance.")
        self._sequence += 1
        identifier = f"sim-message-{self._sequence:04}"
        self.messages[identifier] = {"message_id": identifier, "body": MessageBody,
                                     "receive_count": 0, "available_at": self.now}
        if self.lose_next_send_acknowledgement:
            self.lose_next_send_acknowledgement = False
            raise TimeoutError("Injected lost response after queue acceptance.")
        return {"MessageId": identifier}

    def advance(self, seconds):
        if isinstance(seconds, bool) or not isinstance(seconds, int) or seconds < 0:
            raise ValueError("Advance virtual time by a non-negative integer.")
        self.now += seconds

    def receive_batch(self, limit=10):
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10:
            raise ValueError("Receive between one and ten messages.")
        records = []
        for identifier, message in list(self.messages.items()):
            if message["available_at"] > self.now:
                continue
            if message["receive_count"] >= self.max_receive_count:
                self.dead_letters.append(dict(message))
                del self.messages[identifier]
                continue
            if len(records) >= limit:
                continue
            message["receive_count"] += 1
            message["available_at"] = self.now + self.visibility_timeout_seconds
            message["receipt_handle"] = f"{identifier}:{message['receive_count']}"
            records.append({"messageId": identifier, "body": message["body"],
                            "receiptHandle": message["receipt_handle"],
                            "attributes": {"ApproximateReceiveCount": str(message["receive_count"])}})
        return records

    def acknowledge_batch(self, records, response):
        """Model Lambda's partial batch deletion; listed failures stay invisible."""
        failures = {row["itemIdentifier"] for row in response["batchItemFailures"]}
        identifiers = {record["messageId"] for record in records}
        if not failures <= identifiers:
            raise ValueError("Failure response refers to a message outside this batch.")
        deleted = []
        for record in records:
            identifier = record["messageId"]
            message = self.messages.get(identifier)
            if (identifier not in failures and message
                    and message.get("receipt_handle") == record.get("receiptHandle")):
                del self.messages[identifier]
                deleted.append(identifier)
        return {"deleted": deleted, "retained": sorted(failures)}

    def replay_dead_letter(self, index=0):
        """Deliberately replay one modeled DLQ body as a new source message."""
        message = self.dead_letters[index]
        result = self.send_message(QueueUrl=self.queue_url, MessageBody=message["body"])
        del self.dead_letters[index]
        return result


class _FailedCheck(Exception):
    pass


class _PublisherBoundaryRepository:
    """Use real SQLite transactions without the engine's local auto-publisher.

    The mode selects the same outbox boundary as the AWS adapter; it creates no
    AWS client. Every load and transaction delegates to the temporary SQLite store.
    """

    mode = "aws"

    def __init__(self, sqlite_repository):
        self.sqlite_repository = sqlite_repository
        self.path = sqlite_repository.path

    def load(self):
        return self.sqlite_repository.load()

    def transact(self, mutator):
        return self.sqlite_repository.transact(mutator)


class _ExpectedFailure(logging.Filter):
    def __init__(self, identifier):
        super().__init__()
        self.identifier = identifier

    def filter(self, record):
        # Silence only the deliberately injected failure for this exact record.
        return not (record.msg in {"outbox_publication_failed event_id=%s",
                                   "order_worker_failed message_id=%s"}
                    and record.args == (self.identifier,))


@contextmanager
def _expected_failure(logger_name, identifier):
    logger = logging.getLogger(logger_name)
    expected = _ExpectedFailure(identifier)
    logger.addFilter(expected)
    try:
        yield
    finally:
        logger.removeFilter(expected)


def _check(checks, name, condition, detail, **evidence):
    passed = bool(condition)
    checks.append({"name": name, "passed": passed, "detail": detail, "evidence": evidence})
    if not passed:
        raise _FailedCheck(name)


def _submit(engine, key, sku, quantity=1, scenario="happy_path"):
    order, replayed = engine.submit({"customer": "Reliability rehearsal",
                                    "items": [{"sku": sku, "quantity": quantity}],
                                    "idempotency_key": key, "scenario": scenario}, actor="rehearsal")
    if replayed:
        raise AssertionError("An isolated scenario unexpectedly reused an order.")
    return order["id"]


def _effects(state, identifier):
    return sorted(row["type"] for row in state["effects"].values() if row["order_id"] == identifier)


def _completed(state, identifier, sku, expected_stock):
    validate_state(state)
    return (state["orders"][identifier]["status"] == "completed"
            and state["orders"][identifier]["payment_status"] == "captured"
            and state["orders"][identifier]["shipment_status"] == "shipped"
            and state["work"][identifier]["status"] == "done"
            and _effects(state, identifier) == ["payment", "shipment"]
            and state["inventory"][sku]["on_hand"] == expected_stock
            and state["inventory"][sku]["reserved"] == 0)


def _deliver(queue, engine, process_records, limit=10):
    records = queue.receive_batch(limit)
    if not records:
        raise AssertionError("Expected a visible message in the simulated queue.")
    response = process_records(records, engine)
    acknowledged = queue.acknowledge_batch(records, response)
    return records, response, acknowledged


def _publish_success(repository, queue, publish):
    result = publish(repository, queue, queue.queue_url)
    if result["failed"] or result["pending"] or not result["sent"]:
        raise AssertionError(f"Expected successful publication; got {result}.")
    return result


def _publication_failure(repository, engine, queue, checks, publish, process_records):
    identifier = _submit(engine, "rehearsal-send-failure", "TOTE-001", 2)
    event = next(iter(repository.load()["outbox"].values()))
    queue.fail_next_send = True
    with _expected_failure("aws.handlers.outbox", event["id"]):
        failed = publish(repository, queue, queue.queue_url)
    restarted = Engine(_PublisherBoundaryRepository(SQLiteRepository(repository.path)))
    state = restarted.repository.load()
    _check(checks, "Send failure preserves committed pending work",
           failed == {"sent": 0, "failed": [event["id"]], "pending": 1}
           and state["outbox"][event["id"]]["status"] == "pending"
           and state["orders"][identifier]["status"] == "queued"
           and state["inventory"]["TOTE-001"]["reserved"] == 2
           and not state["effects"] and not queue.messages,
           "Reopening the temporary database retains the order, reservation and pending outbox row.",
           pending_outbox=1, reserved_units=state["inventory"]["TOTE-001"]["reserved"])
    _publish_success(restarted.repository, queue, publish)
    _deliver(queue, restarted, process_records)
    _publish_success(restarted.repository, queue, publish)
    _deliver(queue, restarted, process_records)
    state = repository.load()
    _check(checks, "Outbox repair completes the stranded order",
           _completed(state, identifier, "TOTE-001", 46)
           and all(row["status"] == "sent" for row in state["outbox"].values())
           and not queue.messages and not queue.dead_letters,
           "The real publisher repairs committed work, and the real worker completes payment and shipment.",
           payment_effects=1, shipment_effects=1, on_hand=state["inventory"]["TOTE-001"]["on_hand"])
    return {"send_failures": 1, "final_status": state["orders"][identifier]["status"]}


def _lost_acknowledgement(repository, engine, queue, checks, publish, process_records):
    identifier = _submit(engine, "rehearsal-lost-ack", "NOTE-003", 3)
    event = next(iter(repository.load()["outbox"].values()))
    queue.lose_next_send_acknowledgement = True
    with _expected_failure("aws.handlers.outbox", event["id"]):
        failed = publish(repository, queue, queue.queue_url)
    _check(checks, "Accepted send with lost acknowledgement remains repairable",
           failed == {"sent": 0, "failed": [event["id"]], "pending": 1}
           and len(queue.messages) == 1
           and repository.load()["outbox"][event["id"]]["status"] == "pending",
           "The queue double accepts the body before losing its response; the publisher retains durable pending work.",
           accepted_messages=len(queue.messages), pending_outbox=failed["pending"])
    _publish_success(repository, queue, publish)
    bodies = [message["body"] for message in queue.messages.values()]
    _check(checks, "Outbox repair can create duplicate delivery",
           len(bodies) == 2 and bodies[0] == bodies[1],
           "Repair republishes the same event as a second physical message in the simulation.",
           physical_messages=len(bodies), distinct_event_bodies=len(set(bodies)))
    records, response, acknowledged = _deliver(queue, engine, process_records)
    state = repository.load()
    _check(checks, "Duplicate payment delivery advances one stage once",
           len(records) == 2 and not response["batchItemFailures"] and len(acknowledged["deleted"]) == 2
           and _effects(state, identifier) == ["payment"]
           and state["work"][identifier]["stage"] == "fulfillment"
           and state["inventory"]["NOTE-003"]["on_hand"] == 72
           and state["inventory"]["NOTE-003"]["reserved"] == 3,
           "The worker acknowledges both messages while the stage guard prevents the duplicate from shipping early.",
           payment_deliveries=len(records), payment_effects=len(state["effects"]))
    _publish_success(repository, queue, publish)
    fulfillment_body = next(iter(queue.messages.values()))["body"]
    _deliver(queue, engine, process_records)
    state = repository.load()
    _check(checks, "Duplicate delivery keeps one payment, shipment and stock deduction",
           _completed(state, identifier, "NOTE-003", 69) and not queue.messages,
           "Completion records one payment and one shipment, releases the reservation and deducts three units once.",
           effect_types=_effects(state, identifier), on_hand=state["inventory"]["NOTE-003"]["on_hand"])
    for body in (bodies[0], fulfillment_body):
        queue.send_message(QueueUrl=queue.queue_url, MessageBody=body)
    _, response, acknowledged = _deliver(queue, engine, process_records)
    _check(checks, "Completed stages ignore further duplicate delivery",
           repository.load() == state and not response["batchItemFailures"]
           and len(acknowledged["deleted"]) == 2 and not queue.messages,
           "Replaying payment and fulfillment after completion leaves all domain records unchanged.",
           stale_deliveries=2, domain_state_unchanged=repository.load() == state)
    return {"duplicate_payment_deliveries": 2, "stale_deliveries": 2, "final_status": "completed"}


def _retry_and_recovery(repository, engine, queue, checks, publish, process_records):
    identifier = _submit(engine, "rehearsal-retry-recover", "CHAIR-006", scenario="fulfillment_dead_letter")
    _publish_success(repository, queue, publish)
    _deliver(queue, engine, process_records)
    _publish_success(repository, queue, publish)
    attempts = []
    receive_counts = []
    retained = []
    for receive_number in range(1, 6):
        if receive_number > 1:
            queue.advance(180)
        records, response, acknowledged = _deliver(queue, engine, process_records, 1)
        state = repository.load()
        attempts.append(state["work"][identifier]["attempts"])
        receive_counts.append(int(records[0]["attributes"]["ApproximateReceiveCount"]))
        retained.append(response["batchItemFailures"] == [{"itemIdentifier": records[0]["messageId"]}]
                        and not acknowledged["deleted"])
        if receive_number == 3:
            _check(checks, "Domain retry budget blocks after three provider failures",
                   attempts == [1, 2, 3] and receive_counts == [1, 2, 3] and all(retained)
                   and state["work"][identifier]["status"] == "dead_letter"
                   and state["orders"][identifier]["status"] == "failed"
                   and _effects(state, identifier) == ["payment"]
                   and state["inventory"]["CHAIR-006"]["on_hand"] == 8
                   and state["inventory"]["CHAIR-006"]["reserved"] == 1
                   and len(queue.messages) == 1 and not queue.dead_letters,
                   "Natural redelivery reaches the domain limit before the physical queue model's receive limit.",
                   domain_attempts=attempts[:], source_receive_counts=receive_counts[:], modeled_dlq_depth=0)
    _check(checks, "Blocked work remains failed through five physical receives",
           attempts == [1, 2, 3, 3, 3] and receive_counts == [1, 2, 3, 4, 5] and all(retained)
           and len(queue.messages) == 1 and not queue.dead_letters,
           "The worker keeps returning a partial batch failure after the domain blocks; extra receives add no effects.",
           domain_attempts=attempts, source_receive_counts=receive_counts)
    queue.advance(180)
    redrive_receive = queue.receive_batch()
    _check(checks, "Modeled queue redrive follows five failed receives",
           not redrive_receive and not queue.messages and len(queue.dead_letters) == 1
           and queue.dead_letters[0]["receive_count"] == 5,
           "At virtual second 900 the next poll moves the exhausted source message to the simulated DLQ.",
           modeled_dlq_depth=len(queue.dead_letters), final_receive_count=5, virtual_seconds=queue.now)
    paid = repository.load()["effects"][f"{identifier}:payment"]
    engine.retry(identifier, actor="rehearsal-operator")
    state = repository.load()
    _check(checks, "Operator retry preserves the captured payment and reservation",
           state["work"][identifier]["status"] == "pending" and state["work"][identifier]["attempts"] == 0
           and state["effects"][f"{identifier}:payment"] == paid
           and state["inventory"]["CHAIR-006"]["reserved"] == 1
           and sum(row["status"] == "pending" for row in state["outbox"].values()) == 1,
           "The real retry operation creates new fulfillment work using the original payment and held stock.",
           reset_domain_attempts=state["work"][identifier]["attempts"], pending_outbox=1)
    _publish_success(repository, queue, publish)
    _deliver(queue, engine, process_records)
    state = repository.load()
    _check(checks, "Operator recovery completes exactly once",
           _completed(state, identifier, "CHAIR-006", 7)
           and state["effects"][f"{identifier}:payment"] == paid and not queue.messages,
           "Healthy fulfillment ships one unit and retains the original payment evidence.",
           effect_types=_effects(state, identifier), on_hand=state["inventory"]["CHAIR-006"]["on_hand"])
    queue.replay_dead_letter()
    records, response, acknowledged = _deliver(queue, engine, process_records)
    _check(checks, "Stale DLQ replay after recovery is ignored",
           repository.load() == state and not response["batchItemFailures"]
           and len(acknowledged["deleted"]) == 1 and not queue.messages and not queue.dead_letters
           and records[0]["attributes"]["ApproximateReceiveCount"] == "1",
           "Replaying the old DLQ body after recovery is acknowledged without changing orders, effects or inventory.",
           domain_state_unchanged=repository.load() == state, source_depth=0, modeled_dlq_depth=0)
    return {"domain_attempts": attempts, "source_receive_counts": receive_counts,
            "modeled_redrive_at_virtual_seconds": 900, "final_status": "completed"}


def _poison_message(repository, engine, queue, checks, publish, process_records):
    before = repository.load()
    identifier = queue.send_message(QueueUrl=queue.queue_url, MessageBody="{invalid-json")["MessageId"]
    retained = []
    counts = []
    with _expected_failure("aws.handlers.worker", identifier):
        for receive_number in range(1, 6):
            if receive_number > 1:
                queue.advance(180)
            records, response, acknowledged = _deliver(queue, engine, process_records, 1)
            counts.append(int(records[0]["attributes"]["ApproximateReceiveCount"]))
            retained.append(response["batchItemFailures"] == [{"itemIdentifier": identifier}]
                            and not acknowledged["deleted"] and repository.load() == before)
    queue.advance(180)
    last_receive = queue.receive_batch()
    _check(checks, "Malformed poison message is retained then modeled into the DLQ",
           counts == [1, 2, 3, 4, 5] and all(retained) and not last_receive
           and not queue.messages and len(queue.dead_letters) == 1
           and queue.dead_letters[0]["receive_count"] == 5,
           "Invalid JSON fails the real worker independently of any order and follows the modeled receive policy.",
           source_receive_counts=counts, modeled_dlq_depth=len(queue.dead_letters))
    _check(checks, "Poison message creates no domain work or effects",
           repository.load() == before and not before["orders"] and not before["work"] and not before["effects"],
           "Malformed transport data changes neither inventory nor domain records.",
           orders=0, work_records=0, effects=0, domain_state_unchanged=repository.load() == before)
    return {"source_receive_counts": counts, "modeled_dlq_depth": 1, "domain_orders": 0}


def _mixed_batch(repository, engine, queue, checks, publish, process_records):
    healthy = _submit(engine, "rehearsal-batch-healthy", "MUG-004", 2)
    blocked = _submit(engine, "rehearsal-batch-failed", "TOTE-001", scenario="fulfillment_dead_letter")
    _publish_success(repository, queue, publish)
    _deliver(queue, engine, process_records)
    _publish_success(repository, queue, publish)
    poison = queue.send_message(QueueUrl=queue.queue_url, MessageBody=json.dumps({"order_id": "missing-stage"}))["MessageId"]
    with _expected_failure("aws.handlers.worker", poison):
        records, response, acknowledged = _deliver(queue, engine, process_records)
    valid_ids = {json.loads(record["body"])["order_id"]: record["messageId"]
                 for record in records if record["messageId"] != poison}
    failed_ids = {row["itemIdentifier"] for row in response["batchItemFailures"]}
    state = repository.load()
    _check(checks, "Mixed batch deletes success and retains only failures",
           len(records) == 3 and failed_ids == {valid_ids[blocked], poison}
           and acknowledged["deleted"] == [valid_ids[healthy]]
           and set(queue.messages) == failed_ids
           and _completed(state, healthy, "MUG-004", 33)
           and state["work"][blocked]["attempts"] == 1 and _effects(state, blocked) == ["payment"],
           "In one real worker batch, healthy fulfillment succeeds while a provider failure and invalid envelope remain queued.",
           batch_size=len(records), acknowledged_successes=len(acknowledged["deleted"]), retained_failures=len(queue.messages))
    _check(checks, "Retained failures respect virtual visibility",
           queue.receive_batch() == [] and queue.now == 0,
           "An immediate poll cannot see the two retained messages during their modeled 180-second visibility timeout.",
           invisible_retained_messages=len(queue.messages), visibility_timeout_seconds=180)
    queue.advance(180)
    with _expected_failure("aws.handlers.worker", poison):
        redelivered, response, acknowledged = _deliver(queue, engine, process_records)
    state = repository.load()
    _check(checks, "Mixed batch retry excludes the acknowledged success",
           {record["messageId"] for record in redelivered} == failed_ids
           and all(record["attributes"]["ApproximateReceiveCount"] == "2" for record in redelivered)
           and {row["itemIdentifier"] for row in response["batchItemFailures"]} == failed_ids
           and not acknowledged["deleted"] and state["work"][blocked]["attempts"] == 2
           and _completed(state, healthy, "MUG-004", 33)
           and state["inventory"]["TOTE-001"]["on_hand"] == 48
           and state["inventory"]["TOTE-001"]["reserved"] == 1,
           "Only failed messages return after visibility expires; completed stock and effects stay unchanged.",
           redelivered_failures=len(redelivered), healthy_order_redelivered=False, failed_domain_attempts=2)
    return {"batch_size": 3, "acknowledged_successes": 1, "retained_failures": 2,
            "redelivered_failures": 2, "virtual_seconds": queue.now}


def reliability_rehearsal():
    """Return JSON-compatible integration evidence from disposable local stores.

    The AWS adapters are imported only when this command runs. Their reusable
    publish/process functions require no boto3 installation or AWS credentials.
    Failed checks or unexpected adapter errors yield ``passed=False``.
    """
    from aws.handlers.outbox import publish
    from aws.handlers.worker import process_records

    checks, scenarios = [], []
    cases = (("publication_failure", _publication_failure),
             ("lost_send_acknowledgement", _lost_acknowledgement),
             ("retry_exhaustion_and_recovery", _retry_and_recovery),
             ("malformed_poison_message", _poison_message),
             ("mixed_batch", _mixed_batch))
    temporary_parent = Path.cwd() / "build"
    temporary_parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="orderflow-rehearsal-", dir=temporary_parent) as temporary:
        temporary_path = Path(temporary)
        for name, run in cases:
            first_check = len(checks)
            evidence = {}
            try:
                sqlite_repository = SQLiteRepository(temporary_path / f"{name}.db")
                sqlite_repository.initialize(initial_state())
                repository = _PublisherBoundaryRepository(sqlite_repository)
                engine = Engine(repository)
                evidence = run(repository, engine, SimulatedQueue(), checks, publish, process_records)
                validate_state(repository.load())
            except _FailedCheck:
                pass
            except Exception as error:
                checks.append({"name": f"{name}: execution and state consistency", "passed": False,
                               "detail": f"{type(error).__name__}: {error}", "evidence": {}})
            scenario_checks = checks[first_check:]
            scenarios.append({"name": name, "passed": bool(scenario_checks) and all(row["passed"] for row in scenario_checks),
                              "evidence": evidence})
    removed = not temporary_path.exists()
    checks.append({"name": "Temporary rehearsal databases removed", "passed": removed,
                   "detail": "Every scenario used a fresh disposable SQLite database; all temporary files were removed.",
                   "evidence": {"temporary_storage_removed": removed, "isolated_databases": len(cases)}})
    passed = all(row["passed"] for row in checks) and all(row["passed"] for row in scenarios)
    return {"status": "passed" if passed else "failed", "passed": passed, "scope": "isolated",
            "simulation": True, "checks": checks, "scenarios": scenarios,
            "summary": {"scenarios": len(scenarios), "passed_scenarios": sum(row["passed"] for row in scenarios),
                        "checks": len(checks), "passed_checks": sum(row["passed"] for row in checks)},
            "temporary_storage_removed": removed,
            "queue_model": {"simulation": True, "visibility_timeout_seconds": 180,
                            "max_receive_count": 5, "partial_batch_acknowledgement": True,
                            "redrive": "Next modeled poll after fifth failure and visibility expiry",
                            "clock": "Virtual seconds; no wall-clock sleeps"},
            "limitations": ["The deterministic queue double does not guarantee real SQS delivery, duplicate frequency, visibility or DLQ timing.",
                            "SQLite transactions are exercised; DynamoDB, IAM, Lambda event-source mapping and deployed AWS resources are not tested.",
                            "Payment and shipment effects are simulated; external provider behavior and idempotency require separate verification."]}
