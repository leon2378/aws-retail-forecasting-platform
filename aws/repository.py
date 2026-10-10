"""Bounded entity storage with serializable workspace transactions in DynamoDB.

Each entity is an item, not one growing JSON document. A revision compare-and-swap
serializes demo mutations, including inventory, order and outbox updates. This
deliberately limits throughput and workspace size; it is not a multi-tenant store.
"""
from __future__ import annotations

import copy
import json
import random
import time
import uuid
from decimal import Decimal

from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import ClientError, ConnectionClosedError, EndpointConnectionError, ReadTimeoutError


from .schema import (SECTIONS, MAX_ENTITIES, MAX_STATE_BYTES, MAX_ENTITY_BYTES,
                     MAX_TRANSACTION_ITEMS, MAX_ACCEPTED_ORDERS, PARTITION, META_KEY,
                     RepositoryUnavailable, entity_items)


def _decimal(value):
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {key: _decimal(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_decimal(item) for item in value]
    return value


def _native(value):
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {key: _native(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_native(item) for item in value]
    return value


def encode(item):
    serializer = TypeSerializer()
    return {key: serializer.serialize(_decimal(value)) for key, value in item.items()}


def decode(item):
    deserializer = TypeDeserializer()
    return _native({key: deserializer.deserialize(value) for key, value in item.items()})


class DynamoRepository:
    mode = "aws"

    def __init__(self, table_name, client=None, *, retries=5, sleep=time.sleep):
        if not table_name:
            raise ValueError("ORDERFLOW_TABLE is required.")
        if client is None:
            import boto3
            from botocore.config import Config
            client = boto3.client("dynamodb", config=Config(retries={"mode": "standard", "max_attempts": 3}))
        self.table_name, self.client, self.retries, self.sleep = table_name, client, retries, sleep

    def _revision(self):
        response = self.client.get_item(TableName=self.table_name, Key=encode(META_KEY), ConsistentRead=True)
        item = response.get("Item")
        if not item:
            raise RepositoryUnavailable("Workspace is not initialized. Run the reviewed bootstrap command first.")
        meta = decode(item)
        if type(meta.get("schema")) is not int or meta["schema"] != 1 or type(meta.get("revision")) is not int or meta["revision"] < 0:
            raise RepositoryUnavailable("Unsupported workspace metadata.")
        return meta["revision"]

    def _snapshot(self):
        for attempt in range(self.retries):
            revision = self._revision()
            state = {section: {} for section in SECTIONS}
            request = {"TableName": self.table_name, "KeyConditionExpression": "PK = :pk",
                       "ExpressionAttributeValues": encode({":pk": PARTITION}), "ConsistentRead": True}
            count, size = 0, 0
            while True:
                response = self.client.query(**request)
                for raw in response.get("Items", []):
                    row = decode(raw)
                    if row["SK"] == "META":
                        continue
                    section, key = row["SK"].split("#", 1)
                    if section not in state:
                        raise RepositoryUnavailable("Unsupported stored entity section.")
                    state[section][key] = row["payload"]
                    count += 1
                    size += len(json.dumps(row, ensure_ascii=False, allow_nan=False).encode("utf-8"))
                    if count > MAX_ENTITIES or size > MAX_STATE_BYTES:
                        raise RepositoryUnavailable("Workspace read exceeds the configured capacity limit.")
                if not response.get("LastEvaluatedKey"):
                    break
                request["ExclusiveStartKey"] = response["LastEvaluatedKey"]
            if revision == self._revision():
                return state, revision
            self._pause(attempt)
        raise RepositoryUnavailable("Concurrent writes prevented a consistent snapshot. Retry the request.")

    def snapshot(self):
        """Return all workspace sections and their stable metadata revision."""
        return self._snapshot()

    def load(self):
        return self.snapshot()[0]

    def _pause(self, attempt):
        self.sleep(min(0.02 * (2 ** attempt), 0.25) * random.uniform(0.5, 1.0))

    def _write(self, operations):
        if len(operations) > MAX_TRANSACTION_ITEMS:
            raise RepositoryUnavailable("This mutation exceeds DynamoDB's 100-item transaction limit.")
        token = uuid.uuid4().hex
        # A lost response must retry the identical transaction with the identical
        # token. Re-running the mutator would risk applying a restock twice.
        for attempt in range(self.retries):
            try:
                return self.client.transact_write_items(TransactItems=operations, ClientRequestToken=token)
            except (ReadTimeoutError, ConnectionClosedError, EndpointConnectionError):
                if attempt + 1 == self.retries:
                    raise
                self._pause(attempt)
            except ClientError as exc:
                code = exc.response["Error"]["Code"]
                if code not in {"InternalServerError", "RequestLimitExceeded", "ProvisionedThroughputExceededException", "ThrottlingException"} or attempt + 1 == self.retries:
                    raise
                self._pause(attempt)

    def initialize(self, state):
        rows = entity_items(state)
        operations = [{"Put": {"TableName": self.table_name,
                        "Item": encode({**META_KEY, "schema": 1, "revision": 0}),
                        "ConditionExpression": "attribute_not_exists(PK)"}}]
        operations.extend({"Put": {"TableName": self.table_name, "Item": encode(row),
                                  "ConditionExpression": "attribute_not_exists(PK)"}} for row in rows.values())
        try:
            self._write(operations)
            return True
        except ClientError as exc:
            if self._is_conflict(exc):
                self._revision()
                return False
            raise

    @staticmethod
    def _is_conflict(exc):
        code = exc.response.get("Error", {}).get("Code")
        if code == "TransactionConflictException":
            return True
        if code != "TransactionCanceledException":
            return False
        reasons = exc.response.get("CancellationReasons", [])
        return bool(reasons) and all(reason.get("Code") in {"None", "ConditionalCheckFailed", "TransactionConflict"} for reason in reasons)

    def transact(self, mutator):
        for attempt in range(self.retries):
            before, revision = self._snapshot()
            after = copy.deepcopy(before)
            result = mutator(after)
            original, current = entity_items(before), entity_items(after)
            if after["orders"].keys() - before["orders"].keys():
                self._check_admission(after, current)
            changed = [key for key, item in current.items() if original.get(key) != item]
            removed = original.keys() - current.keys()
            if not changed and not removed:
                return copy.deepcopy(result)
            operations = [{"Update": {"TableName": self.table_name, "Key": encode(META_KEY),
                "UpdateExpression": "SET revision = :next", "ConditionExpression": "revision = :expected",
                "ExpressionAttributeValues": encode({":expected": revision, ":next": revision + 1})}}]
            operations.extend({"Put": {"TableName": self.table_name, "Item": encode(current[key])}} for key in changed)
            operations.extend({"Delete": {"TableName": self.table_name, "Key": encode({"PK": PARTITION, "SK": key})}} for key in sorted(removed))
            try:
                self._write(operations)
                return copy.deepcopy(result)
            except ClientError as exc:
                if not self._is_conflict(exc):
                    raise
                self._pause(attempt)
        raise RepositoryUnavailable("Workspace is busy. Retry the request.")

    @staticmethod
    def _check_admission(state, rows):
        if len(state["orders"]) > MAX_ACCEPTED_ORDERS:
            raise RepositoryUnavailable("AWS demo order capacity reached. Existing orders remain recoverable.")
        unfinished = sum(row.get("status") != "done" for row in state["work"].values())
        missing_events = max(0, 1000 - len(state["events"]))
        missing_drills = max(0, 20 - len(state["drills"]))
        # Reserve room for payment/shipment/outbox records, bounded audit growth
        # and recovery evidence before accepting another inventory reservation.
        projected_entities = len(rows) + unfinished * 4 + missing_events + missing_drills
        stored_bytes = sum(len(json.dumps(row, ensure_ascii=False, allow_nan=False).encode("utf-8")) for row in rows.values())
        projected_bytes = stored_bytes + unfinished * 12_000 + missing_events * 1000 + missing_drills * 12_000
        if projected_entities > MAX_ENTITIES or projected_bytes > MAX_STATE_BYTES:
            raise RepositoryUnavailable("Insufficient storage headroom for another order and its recovery records.")
