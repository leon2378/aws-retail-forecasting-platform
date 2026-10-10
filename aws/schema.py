"""Credential-free validation of the bounded AWS workspace format."""
import json


SECTIONS = ("inventory", "orders", "work", "effects", "idempotency", "events", "outbox", "drills")
MAX_ENTITIES = 2000
MAX_STATE_BYTES = 3_000_000
MAX_ENTITY_BYTES = 320_000
MAX_TRANSACTION_ITEMS = 100
MAX_ACCEPTED_ORDERS = 100
PARTITION = "WORKSPACE"
META_KEY = {"PK": PARTITION, "SK": "META"}


class RepositoryUnavailable(RuntimeError):
    """Storage is unavailable, not initialized or has exceeded a stated limit."""


def entity_items(state):
    if set(state) != set(SECTIONS) or not all(isinstance(state[section], dict) for section in SECTIONS):
        raise RepositoryUnavailable("Unsupported workspace schema.")
    rows = {}
    total_bytes = 0
    for section in SECTIONS:
        for key, payload in state[section].items():
            if not isinstance(key, str) or not key or len(key.encode("utf-8")) > 800:
                raise RepositoryUnavailable("Invalid entity key.")
            size = len(json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8"))
            if size > MAX_ENTITY_BYTES:
                raise RepositoryUnavailable("An entity exceeds the demo item size limit.")
            sk = f"{section}#{key}"
            rows[sk] = {"PK": PARTITION, "SK": sk, "payload": payload}
            total_bytes += size + len(sk.encode("utf-8")) + 100
    if len(rows) > MAX_ENTITIES or total_bytes > MAX_STATE_BYTES:
        raise RepositoryUnavailable("Workspace capacity reached; archive data before accepting more orders.")
    return rows
