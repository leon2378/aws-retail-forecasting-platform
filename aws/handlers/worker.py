"""At-least-once SQS work with stage guards and partial batch failure reporting."""
import json
import logging
import os

log = logging.getLogger(__name__)
log.setLevel(logging.INFO)


def process_records(records, engine):
    failures = []
    for record in records:
        try:
            message = json.loads(record["body"])
            if (not isinstance(message, dict) or set(message) != {"order_id", "stage", "event_id"}
                    or message["stage"] not in {"payment", "fulfillment"}
                    or not all(isinstance(value, str) and value for value in message.values())):
                raise ValueError("Invalid work message.")
            result = engine.process_order(message["order_id"], actor="worker", expected_stage=message["stage"])
            attributes = record.get("attributes")
            received = attributes.get("ApproximateReceiveCount") if isinstance(attributes, dict) else None
            receive_count = (int(received) if isinstance(received, str) and 0 < len(received) <= 10
                             and received.isascii() and received.isdecimal() else None)
            log.info("order_work_result %s", json.dumps({
                "message_id": record["messageId"], "order_id": message["order_id"],
                "event_id": message["event_id"], "stage": message["stage"],
                "receive_count": receive_count, "outcome": result["outcome"]}, separators=(",", ":")))
            if result["outcome"] in {"retry", "dead_letter"}:
                failures.append({"itemIdentifier": record["messageId"]})
        except Exception:
            log.exception("order_worker_failed message_id=%s", record.get("messageId", "unknown"))
            failures.append({"itemIdentifier": record["messageId"]})
    return {"batchItemFailures": failures}


def handler(event, context):
    from aws.repository import DynamoRepository
    from orderflow.engine import Engine
    return process_records(event.get("Records", []), Engine(DynamoRepository(os.environ["ORDERFLOW_TABLE"])))
