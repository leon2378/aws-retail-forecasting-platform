"""Isolated, repeatable recovery evidence using the real local state machine."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import uuid
import os

from .engine import Engine, timestamp, validate_state
from .errors import Conflict
from .storage import SQLiteRepository


def recovery_drill(drill_id=None):
    started = time.monotonic()
    identifier = drill_id or uuid.uuid4().hex
    checks, steps = [], []

    def check(name, passed, detail):
        checks.append({"name": name, "passed": bool(passed), "detail": detail})

    temporary_parent = Path("/tmp") if os.environ.get("AWS_LAMBDA_FUNCTION_NAME") else Path.cwd() / "build"
    temporary_parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="orderflow-drill-", dir=temporary_parent) as temporary:
        repository = SQLiteRepository(Path(temporary) / "isolated.db")
        engine = Engine(repository)
        repository.transact(lambda state: state["inventory"]["TOTE-001"].update(on_hand=1))

        def compete(index):
            payload = {"customer": f"Drill customer {index}", "items": [{"sku": "TOTE-001", "quantity": 1}],
                       "scenario": "fulfillment_dead_letter", "idempotency_key": f"race-order-{index}"}
            try:
                order, _ = engine.submit(payload, "drill")
                return {"order": order, "payload": payload}
            except Conflict as error:
                return {"code": error.code}

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(compete, (1, 2)))
        winners = [result for result in results if "order" in result]
        state = repository.load()
        check("Last-unit race", len(winners) == 1 and state["inventory"]["TOTE-001"]["reserved"] == 1
              and sum(r.get("code") == "insufficient_stock" for r in results) == 1,
              "Two request threads competed for one unit; exactly one reservation committed.")
        steps.append({"title": "Compete for the last unit", "detail": "Serializable database transactions protect the inventory reservation."})
        if winners:
            winner = winners[0]
            order_id = winner["order"]["id"]
            repeated, replayed = engine.submit(winner["payload"], "drill")
            check("Duplicate request", replayed and repeated["id"] == order_id and len(repository.load()["orders"]) == 1,
                  "The same identity and request key return the existing order without reserving again.")
            changed = {**winner["payload"], "customer": "A different payload"}
            conflicted = False
            try:
                engine.submit(changed, "drill")
            except Conflict as error:
                conflicted = error.code == "idempotency_conflict"
            check("Conflicting request key", conflicted, "Changing the payload while reusing its request key is rejected.")
            engine.process_order(order_id, "drill", expected_stage="payment", crash_after_effect=True)
            engine.process_order(order_id, "drill", expected_stage="payment")
            state = repository.load()
            check("Interruption after payment", len([e for e in state["effects"].values() if e["type"] == "payment"]) == 1
                  and state["orders"][order_id]["payment_status"] == "captured",
                  "Redelivery after effect commit reuses the payment record; one simulated charge exists.")
            steps.append({"title": "Interrupt and redeliver payment", "detail": "A persisted effect survives a worker interruption and prevents duplicate capture."})
            for _ in range(3):
                engine.process_order(order_id, "drill", expected_stage="fulfillment")
            state = repository.load()
            check("Bounded failure handling", state["work"][order_id]["status"] == "dead_letter"
                  and state["work"][order_id]["attempts"] == 3 and state["inventory"]["TOTE-001"]["reserved"] == 1,
                  "Three simulated provider failures block work while preserving the paid order's reservation.")
            engine.retry(order_id, "drill")
            engine.process_order(order_id, "drill", expected_stage="fulfillment")
            engine.process_order(order_id, "drill", expected_stage="fulfillment")
            state = repository.load()
            check("Safe recovery", state["orders"][order_id]["status"] == "completed"
                  and state["inventory"]["TOTE-001"]["on_hand"] == 0 and state["inventory"]["TOTE-001"]["reserved"] == 0
                  and len(state["effects"]) == 2,
                  "Manual recovery completes one shipment and deducts stock once, using the original payment.")
            steps.append({"title": "Repair the provider and resume", "detail": "The recovery reuses existing reservation and payment evidence."})

        cancellation, _ = engine.submit({"customer": "Compensation drill", "items": [{"sku": "NOTE-003", "quantity": 2}],
            "idempotency_key": "compensation-drill"}, "drill")
        engine.process_order(cancellation["id"], "drill")
        engine.cancel(cancellation["id"], "drill")
        engine.cancel(cancellation["id"], "drill")
        state = repository.load()
        check("Repeatable compensation", state["inventory"]["NOTE-003"]["reserved"] == 0
              and state["inventory"]["NOTE-003"]["on_hand"] == 72
              and sum(e["type"] == "refund" for e in state["effects"].values()) == 1,
              "Repeated cancellation releases stock and records one simulated refund.")
        backup = repository.backup(Path(temporary) / "verified-backup.db")
        engine.restock("NOTE-003", 5, "drill")
        repository.restore(backup)
        check("Backup restoration", repository.load() == state, "A validated SQLite backup restores inventory, orders, effects and outbox evidence.")
        validate_state(repository.load())
        check("State consistency", True, "Active reservations agree with inventory; completed orders have payment and shipment records.")
        steps.append({"title": "Validate compensation and restoration", "detail": "Repeated cancellation and a backup restore preserve the ledger's invariants."})
    passed = all(check["passed"] for check in checks)
    return {"id": identifier, "status": "passed" if passed else "failed", "passed": passed,
            "at": timestamp(), "duration_ms": round((time.monotonic() - started) * 1000),
            "checks": checks, "steps": steps, "scope": "isolated", "simulation": True,
            "limitations": "This exercises real local database transactions and simulated providers. AWS queue delivery, IAM and regional capacity are not tested."}
