"""At-least-once SQS work with stage guards and partial batch failure reporting."""
import json
import logging
import os

from aws.repository import DynamoRepository

log = logging.getLogger(__name__)


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
            if result["outcome"] in {"retry", "dead_letter"}:
                failures.append({"itemIdentifier": record["messageId"]})
        except Exception:
            log.exception("order_worker_failed message_id=%s", record.get("messageId", "unknown"))
            failures.append({"itemIdentifier": record["messageId"]})
    return {"batchItemFailures": failures}


def handler(event, context):
    from orderflow.engine import Engine
    return process_records(event.get("Records", []), Engine(DynamoRepository(os.environ["ORDERFLOW_TABLE"])))
