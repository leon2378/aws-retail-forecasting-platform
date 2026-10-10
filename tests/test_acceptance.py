"""Independent acceptance checks for inventory and recovery invariants."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import patch
import unittest

from orderflow.engine import Engine, validate_state
from orderflow.errors import Conflict, OrderFlowError
from orderflow.storage import SQLiteRepository


class OrderFlowAcceptance(unittest.TestCase):
    def setUp(self):
        scratch = Path(__file__).resolve().parents[1] / "build"
        scratch.mkdir(exist_ok=True)
        self.workspace = TemporaryDirectory(dir=scratch)
        self.database = Path(self.workspace.name) / "orders.db"
        self.repository = SQLiteRepository(self.database)
        self.engine = Engine(self.repository)

    def tearDown(self):
        self.workspace.cleanup()

    def payload(self, key="acceptance-request-001", *, quantity=1, scenario="happy_path", sku="CHAIR-006"):
        return {"customer": "Example Studio", "items": [{"sku": sku, "quantity": quantity}],
                "idempotency_key": key, "scenario": scenario}

    def assert_consistent(self):
        state = self.repository.load()
        validate_state(state)
        for product in state["inventory"].values():
            self.assertGreaterEqual(product["on_hand"] - product["reserved"], 0)
        return state

    def test_order_reservation_and_committed_work_survive_restart(self):
        order, replayed = self.engine.submit(self.payload(quantity=2))
        self.assertFalse(replayed)
        restarted = Engine(SQLiteRepository(self.database))
        state = self.assert_consistent()
        self.assertEqual(state["inventory"]["CHAIR-006"]["reserved"], 2)
        self.assertEqual(state["work"][order["id"]]["stage"], "payment")
        self.assertEqual([(row["order_id"], row["stage"]) for row in state["outbox"].values()],
                         [(order["id"], "payment")])
        restarted.process()
        restarted.process()
        self.assertEqual(restarted.get_order(order["id"])["status"], "completed")
        self.assertEqual(self.repository.load()["inventory"]["CHAIR-006"]["on_hand"], 6)
        self.assert_consistent()

    def test_concurrent_requests_for_last_unit_have_one_winner(self):
        self.repository.transact(lambda state: state["inventory"]["CHAIR-006"].update(on_hand=1))
        ready = Barrier(2)

        def reserve(index):
            engine = Engine(SQLiteRepository(self.database))
            ready.wait(timeout=5)
            try:
                return engine.submit(self.payload(f"last-unit-request-{index:03}"))[0]["id"]
            except Conflict as error:
                return error.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(reserve, range(2)))
        self.assertEqual(results.count("insufficient_stock"), 1)
        state = self.assert_consistent()
        self.assertEqual(len(state["orders"]), 1)
        self.assertEqual(state["inventory"]["CHAIR-006"]["reserved"], 1)

    def test_concurrent_duplicate_requests_create_one_order(self):
        ready = Barrier(2)

        def submit(_):
            engine = Engine(SQLiteRepository(self.database))
            ready.wait(timeout=5)
            return engine.submit(self.payload())

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(submit, range(2)))
        self.assertEqual(results[0][0]["id"], results[1][0]["id"])
        self.assertEqual(sorted(replayed for _, replayed in results), [False, True])
        state = self.assert_consistent()
        self.assertEqual(len(state["orders"]), 1)
        self.assertEqual(len(state["outbox"]), 1)
        self.assertEqual(state["inventory"]["CHAIR-006"]["reserved"], 1)

    def test_changed_request_with_same_key_conflicts_without_mutation(self):
        self.engine.submit(self.payload())
        before = self.repository.load()
        with self.assertRaises(Conflict) as caught:
            self.engine.submit(self.payload(quantity=2))
        self.assertEqual(caught.exception.code, "idempotency_conflict")
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.repository.load(), before)

    def test_different_identities_do_not_share_request_keys(self):
        first, first_replayed = self.engine.submit(self.payload(), actor="operator-one")
        second, second_replayed = self.engine.submit(self.payload(), actor="operator-two")
        self.assertFalse(first_replayed)
        self.assertFalse(second_replayed)
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(self.repository.load()["inventory"]["CHAIR-006"]["reserved"], 2)
        self.assert_consistent()

    def test_multiline_insufficient_stock_reserves_nothing(self):
        payload = self.payload()
        payload["items"] = [{"sku": "TOTE-001", "quantity": 2}, {"sku": "CHAIR-006", "quantity": 9}]
        before = self.repository.load()
        with self.assertRaises(Conflict):
            self.engine.submit(payload)
        self.assertEqual(self.repository.load(), before)

    def test_payment_decline_releases_stock_without_effects(self):
        order, _ = self.engine.submit(self.payload(quantity=2, scenario="payment_declined"))
        self.engine.process_order(order["id"])
        state = self.assert_consistent()
        self.assertEqual(state["orders"][order["id"]]["payment_status"], "declined")
        self.assertFalse(state["orders"][order["id"]]["reservation_active"])
        self.assertEqual(state["inventory"]["CHAIR-006"]["on_hand"], 8)
        self.assertEqual(state["inventory"]["CHAIR-006"]["reserved"], 0)
        self.assertEqual(state["effects"], {})

    def test_worker_redelivery_after_effect_does_not_charge_or_ship_twice(self):
        order, _ = self.engine.submit(self.payload(quantity=2))
        identifier = order["id"]
        self.assertEqual(self.engine.process_order(identifier, crash_after_effect=True)["outcome"], "retry")
        self.assertEqual(self.repository.load()["work"][identifier]["stage"], "payment")
        self.assertIn(f"{identifier}:payment", self.repository.load()["effects"])
        self.engine.process_order(identifier)
        self.engine.process_order(identifier, crash_after_effect=True)
        self.engine.process_order(identifier)
        self.assertEqual(self.engine.process_order(identifier)["outcome"], "ignored")
        state = self.assert_consistent()
        self.assertEqual(sorted(row["type"] for row in state["effects"].values()), ["payment", "shipment"])
        self.assertEqual(state["inventory"]["CHAIR-006"]["on_hand"], 6)
        self.assertEqual(state["inventory"]["CHAIR-006"]["reserved"], 0)
        effect_events = [row for row in state["events"].values() if row["type"].endswith(".effect")]
        self.assertEqual(len(effect_events), 2)

    def test_stale_payment_delivery_cannot_advance_fulfillment(self):
        order, _ = self.engine.submit(self.payload())
        self.engine.process_order(order["id"], expected_stage="payment")
        before = self.repository.load()
        result = self.engine.process_order(order["id"], expected_stage="payment")
        self.assertEqual(result["outcome"], "ignored")
        self.assertEqual(self.repository.load(), before)

    def test_three_failed_attempts_block_and_recovery_keeps_one_payment(self):
        order, _ = self.engine.submit(self.payload(scenario="fulfillment_dead_letter"))
        identifier = order["id"]
        self.engine.process_order(identifier)
        outcomes = [self.engine.process_order(identifier)["outcome"] for _ in range(3)]
        self.assertEqual(outcomes, ["retry", "retry", "dead_letter"])
        blocked = self.assert_consistent()
        self.assertEqual(blocked["work"][identifier]["attempts"], 3)
        self.assertTrue(blocked["orders"][identifier]["reservation_active"])
        self.engine.retry(identifier, actor="admin")
        self.engine.process_order(identifier)
        state = self.assert_consistent()
        self.assertEqual(state["orders"][identifier]["status"], "completed")
        self.assertEqual(sorted(row["type"] for row in state["effects"].values()), ["payment", "shipment"])
        self.assertEqual(state["inventory"]["CHAIR-006"]["on_hand"], 7)

    def test_cancellation_after_payment_refunds_and_releases_once(self):
        order, _ = self.engine.submit(self.payload(quantity=2))
        identifier = order["id"]
        self.engine.process_order(identifier, crash_after_effect=True)
        first = self.engine.cancel(identifier)
        second = self.engine.cancel(identifier)
        self.assertEqual(first, second)
        self.assertEqual(self.engine.process_order(identifier)["outcome"], "ignored")
        state = self.assert_consistent()
        self.assertEqual(sorted(row["type"] for row in state["effects"].values()), ["payment", "refund"])
        self.assertEqual(state["inventory"]["CHAIR-006"]["on_hand"], 8)
        self.assertEqual(state["inventory"]["CHAIR-006"]["reserved"], 0)

    def test_committed_shipment_cannot_be_cancelled_after_interruption(self):
        order, _ = self.engine.submit(self.payload())
        self.engine.process_order(order["id"])
        self.engine.process_order(order["id"], crash_after_effect=True)
        before = self.repository.load()
        with self.assertRaises(Conflict) as caught:
            self.engine.cancel(order["id"])
        self.assertEqual(caught.exception.code, "already_shipped")
        self.assertEqual(self.repository.load(), before)
        self.engine.process_order(order["id"])
        self.assert_consistent()

    def test_invalid_quantities_have_no_domain_side_effect(self):
        for quantity in (True, 0, -1, 1.5, "2", 101):
            with self.subTest(quantity=quantity):
                before = self.repository.load()
                with self.assertRaises(OrderFlowError) as caught:
                    self.engine.submit(self.payload(quantity=quantity))
                self.assertEqual(caught.exception.status, 400)
                self.assertEqual(self.repository.load(), before)

    def test_backup_restore_retains_idempotency_and_effect_records(self):
        payload = self.payload()
        order, _ = self.engine.submit(payload)
        self.engine.process_order(order["id"], crash_after_effect=True)
        snapshot = self.repository.load()
        backup = self.repository.backup(Path(self.workspace.name) / "backup.db")
        self.engine.process_order(order["id"])
        self.engine.process_order(order["id"])
        self.repository.restore(backup)
        self.assertEqual(self.repository.load(), snapshot)
        existing, replayed = self.engine.submit(payload)
        self.assertTrue(replayed)
        self.assertEqual(existing["id"], order["id"])
        self.engine.process_order(order["id"])
        self.engine.process_order(order["id"])
        state = self.assert_consistent()
        self.assertEqual(len(state["effects"]), 2)
        self.assertEqual(state["inventory"]["CHAIR-006"]["on_hand"], 7)

    def test_restore_rejects_corrupt_work_effect_and_outbox_before_live_changes(self):
        order, _ = self.engine.submit(self.payload())
        identifier = order["id"]
        self.engine.process_order(identifier)
        healthy = self.repository.load()
        corruptions = {
            "work_stage": lambda state: state["work"][identifier].update(stage="unknown"),
            "effect_identity": lambda state: state["effects"][f"{identifier}:payment"].update(order_id="missing-order"),
            "outbox_identity": lambda state: next(iter(state["outbox"].values())).update(order_id="missing-order"),
        }
        for name, corrupt in corruptions.items():
            with self.subTest(corruption=name):
                candidate = self.repository.backup(Path(self.workspace.name) / f"corrupt-{name}.db")
                SQLiteRepository(candidate).transact(corrupt)
                with self.assertRaises(ValueError):
                    self.repository.restore(candidate)
                self.assertEqual(self.repository.load(), healthy)

    def test_retention_keeps_recent_events_after_serialized_reload(self):
        times = [f"2026-10-09T00:00:00.00{index}Z" for index in range(6)]
        # IDs deliberately sort opposite to creation time: persistence must not
        # turn dictionary key order into an audit retention policy.
        identifiers = [SimpleNamespace(hex=letter * 32) for letter in "fedcba"]
        with patch("orderflow.engine.MAX_EVENTS", 3), \
                patch("orderflow.engine.timestamp", side_effect=times), \
                patch("orderflow.engine.uuid.uuid4", side_effect=identifiers):
            for index in range(6):
                self.engine.restock("TOTE-001", 1, actor=f"retention-{index}")
        events = self.repository.load()["events"].values()
        self.assertEqual(sorted(event["at"] for event in events), times[-3:])
        self.assert_consistent()


if __name__ == "__main__":
    unittest.main()
