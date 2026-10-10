"""Publish committed work. Sending before marking allows safe duplicate delivery."""
import json
import logging
import os
from datetime import datetime, timezone

log = logging.getLogger(__name__)


def publish(repository, queue, queue_url, *, event_ids=None, limit=100):
    pending = [row for row in repository.load()["outbox"].values()
               if row.get("status") == "pending" and (event_ids is None or row["id"] in event_ids)]
    sent = 0
    failures = []
    for entry in sorted(pending, key=lambda row: (row.get("created_at", ""), row["id"]))[:limit]:
        try:
            queue.send_message(QueueUrl=queue_url, MessageBody=json.dumps({
                "order_id": entry["order_id"], "stage": entry["stage"], "event_id": entry["id"]}, separators=(",", ":")))

            def acknowledge(state):
                row = state["outbox"].get(entry["id"])
                if row and row.get("status") == "pending":
                    row["status"] = "sent"
                    row["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
                return None

            repository.transact(acknowledge)
            sent += 1
        except Exception:
            log.exception("outbox_publication_failed event_id=%s", entry["id"])
            failures.append(entry["id"])
    return {"sent": sent, "failed": failures, "pending": max(0, len(pending) - sent)}


def handler(event, context):
    import boto3
    from aws.repository import DynamoRepository, decode
    repository = DynamoRepository(os.environ["ORDERFLOW_TABLE"])
    records = event.get("Records")
    if records is None:
        result = publish(repository, boto3.client("sqs"), os.environ["WORK_QUEUE_URL"])
        if result["failed"]:
            raise RuntimeError("Outbox repair left failed publications; committed work remains pending.")
        return result
    selected, sequences = set(), {}
    for record in records:
        if record.get("eventName") not in {"INSERT", "MODIFY"}:
            continue
        raw = record.get("dynamodb", {}).get("NewImage")
        if not raw:
            continue
        row = decode(raw)
        if row.get("SK", "").startswith("outbox#") and row.get("payload", {}).get("status") == "pending":
            entry = row["payload"]
            selected.add(entry["id"])
            sequences[entry["id"]] = record["dynamodb"]["SequenceNumber"]
    if not selected:
        return {"batchItemFailures": []}
    result = publish(repository, boto3.client("sqs"), os.environ["WORK_QUEUE_URL"], event_ids=selected)
    return {"batchItemFailures": [{"itemIdentifier": sequences[key]} for key in result["failed"]]}
