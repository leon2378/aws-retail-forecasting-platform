"""Order state machine with atomic inventory, effect records and a durable outbox.

Payment, refund and shipping adapters are deliberately simulated. Their effect
records and order changes commit together. Real providers require an external
idempotency key, reconciliation and authenticated webhooks before replacement.
"""

from datetime import datetime, timezone
from hashlib import sha256
import json
import re
import time
import uuid

from .errors import Conflict, NotFound, OrderFlowError

SECTIONS = ("inventory", "orders", "work", "effects", "idempotency", "events", "outbox", "drills")
SCENARIOS = ("happy_path", "payment_declined", "fulfillment_retry", "fulfillment_dead_letter")
MAX_ORDERS, MAX_EVENTS, MAX_DRILLS = 250, 1000, 20
KEY_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,128}$")


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def initial_state():
    state = {section: {} for section in SECTIONS}
    for sku, name, category, price, units, reorder in (
        ("TOTE-001", "Everyday canvas tote", "Accessories", 2400, 48, 12),
        ("LAMP-002", "Arc desk lamp", "Home", 4800, 26, 8),
        ("NOTE-003", "Field notes journal", "Stationery", 1200, 72, 20),
        ("MUG-004", "Trail insulated mug", "Accessories", 2200, 35, 10),
        ("AUDIO-005", "Studio wireless headphones", "Electronics", 8900, 18, 5),
        ("CHAIR-006", "Form studio chair", "Home", 12900, 8, 3),
    ):
        state["inventory"][sku] = {"sku": sku, "name": name, "category": category,
                                   "price_cents": price, "on_hand": units,
                                   "reserved": 0, "reorder_level": reorder}
    return state


def _copy(value):
    return json.loads(json.dumps(value, allow_nan=False))


def _integer(value, name, minimum=1, maximum=1000):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise OrderFlowError(400, "invalid_input", f"{name} must be an integer from {minimum} to {maximum}.")
    return value


def _event(state, order, kind, title, detail, actor, at):
    event = {"id": uuid.uuid4().hex, "order_id": order["id"] if order else None,
             "type": kind, "title": title, "detail": detail, "actor": actor, "at": at}
    state["events"][event["id"]] = event
    while len(state["events"]) > MAX_EVENTS:
        oldest = min(state["events"], key=lambda key: state["events"][key]["at"])
        del state["events"][oldest]
    if order is not None:
        order["timeline"].append(event)
        order["timeline"] = order["timeline"][-60:]
        order["updated_at"] = at
    return event


def _enqueue(state, order, stage, at):
    identifier = uuid.uuid4().hex
    state["outbox"][identifier] = {"id": identifier, "order_id": order["id"],
                                    "type": "work.available", "stage": stage,
                                    "status": "pending", "created_at": at, "updated_at": at}


def _release(state, order):
    if not order["reservation_active"]:
        return
    for item in order["items"]:
        state["inventory"][item["sku"]]["reserved"] -= item["quantity"]
    order["reservation_active"] = False


def _order(state, identifier):
    if identifier not in state["orders"]:
        raise NotFound("That order does not exist.")
    return state["orders"][identifier]


def _product(value):
    return {**value, "available": value["on_hand"] - value["reserved"]}


def validate_state(state):
    """Reject corrupt backups and check reservation/effect consistency."""
    if not isinstance(state, dict) or set(state) != set(SECTIONS) or any(not isinstance(state[s], dict) for s in SECTIONS):
        raise ValueError("Workspace schema is invalid.")
    if len(state["orders"]) > MAX_ORDERS or len(state["events"]) > MAX_EVENTS or len(state["drills"]) > MAX_DRILLS:
        raise ValueError("Workspace exceeds the reference application's retention limits.")
    reserved = {sku: 0 for sku in state["inventory"]}
    for sku, product in state["inventory"].items():
        if not isinstance(product, dict) or product.get("sku") != sku:
            raise ValueError("Inventory identity is invalid.")
        for field in ("on_hand", "reserved", "price_cents", "reorder_level"):
            value = product.get(field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("Inventory quantities and prices must be non-negative integers.")
        if product["reserved"] > product["on_hand"]:
            raise ValueError("Reserved stock exceeds inventory.")
    for identifier, order in state["orders"].items():
        if not isinstance(order, dict) or order.get("id") != identifier or order.get("status") not in ("queued", "processing", "completed", "cancelled", "failed"):
            raise ValueError("Order identity or state is invalid.")
        if not isinstance(order.get("reservation_active"), bool) or not isinstance(order.get("items"), list) or not order["items"]:
            raise ValueError("Order reservation or items are invalid.")
        total = 0
        for item in order["items"]:
            if not isinstance(item, dict):
                raise ValueError("Order item is invalid.")
            sku, quantity, price = item.get("sku"), item.get("quantity"), item.get("price_cents")
            if sku not in reserved or isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
                raise ValueError("Order inventory references are invalid.")
            if isinstance(price, bool) or not isinstance(price, int) or price < 0:
                raise ValueError("Order price is invalid.")
            total += quantity * price
            if order["reservation_active"]:
                reserved[sku] += quantity
        if total != order.get("total_cents") or identifier not in state["work"]:
            raise ValueError("Order total or work record is invalid.")
        if order["status"] in ("completed", "cancelled") and order["reservation_active"]:
            raise ValueError("Terminal order still reserves stock.")
        if order["status"] == "completed" and (f"{identifier}:payment" not in state["effects"] or f"{identifier}:shipment" not in state["effects"]):
            raise ValueError("Completed order has no payment or shipment evidence.")
    if any(reserved[sku] != state["inventory"][sku]["reserved"] for sku in reserved):
        raise ValueError("Inventory does not agree with active reservations.")
    def valid_time(value):
        if not isinstance(value, str) or len(value) > 40:
            raise ValueError("Record timestamp is invalid.")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError("Timestamp needs a timezone.")
        except ValueError as error:
            raise ValueError("Record timestamp is invalid.") from error

    def valid_event(key, event):
        if not isinstance(event, dict) or event.get("id") != key:
            raise ValueError("Audit event identity is invalid.")
        if event.get("order_id") is not None and event.get("order_id") not in state["orders"]:
            raise ValueError("Audit event refers to a missing order.")
        for field in ("type", "title", "detail", "actor"):
            if not isinstance(event.get(field), str) or len(event[field]) > 1000:
                raise ValueError("Audit event fields are invalid.")
        valid_time(event.get("at"))

    for identifier, order in state["orders"].items():
        work = state["work"][identifier]
        if not isinstance(work, dict) or work.get("order_id") != identifier or work.get("id") != "work-" + identifier:
            raise ValueError("Work identity is invalid.")
        if work.get("stage") not in ("payment", "fulfillment", "done") or work.get("stage") != order.get("stage"):
            raise ValueError("Work stage does not agree with the order.")
        if work.get("status") not in ("pending", "dead_letter", "done"):
            raise ValueError("Work status is invalid.")
        attempts = work.get("attempts")
        if isinstance(attempts, bool) or not isinstance(attempts, int) or not 0 <= attempts <= 3 or work.get("max_attempts") != 3:
            raise ValueError("Work retry policy is invalid.")
        status, stage = order["status"], order["stage"]
        allowed = {"queued": ("pending", "payment", True), "processing": ("pending", "fulfillment", True),
                   "completed": ("done", "done", False), "cancelled": ("done", "done", False)}
        if status in allowed and (work["status"], stage, order["reservation_active"]) != allowed[status]:
            raise ValueError("Order and work lifecycle disagree.")
        if status == "failed":
            declined = work["status"] == "done" and stage == "done" and not order["reservation_active"] and order.get("payment_status") == "declined"
            blocked = work["status"] == "dead_letter" and stage == "fulfillment" and order["reservation_active"] and attempts == 3
            if not (declined or blocked):
                raise ValueError("Failed order is inconsistent with compensation or blocked work.")
        if order.get("payment_status") not in ("pending", "captured", "declined", "cancelled", "refunded") or order.get("shipment_status") not in ("pending", "shipped", "cancelled"):
            raise ValueError("Provider status is invalid.")
        if order.get("payment_status") in ("captured", "refunded") and f"{identifier}:payment" not in state["effects"]:
            raise ValueError("Captured payment has no effect record.")
        if order.get("payment_status") == "refunded" and f"{identifier}:refund" not in state["effects"]:
            raise ValueError("Refund has no effect record.")
        for record in (order, work):
            valid_time(record.get("created_at"))
            valid_time(record.get("updated_at"))
        if not isinstance(order.get("timeline"), list) or len(order["timeline"]) > 60:
            raise ValueError("Order timeline is invalid.")
        for event in order["timeline"]:
            if not isinstance(event, dict):
                raise ValueError("Order timeline event is invalid.")
            valid_event(event.get("id"), event)
            if event.get("order_id") != identifier:
                raise ValueError("Timeline belongs to a different order.")
    if set(state["work"]) != set(state["orders"]):
        raise ValueError("Work records do not agree with orders.")
    for key, effect in state["effects"].items():
        if not isinstance(effect, dict) or effect.get("order_id") not in state["orders"]:
            raise ValueError("Effect refers to a missing order.")
        identifier, kind = effect["order_id"], effect.get("type")
        if kind not in ("payment", "shipment", "refund") or effect.get("id") != key or key != f"{identifier}:{kind}":
            raise ValueError("Effect identity or type is invalid.")
        if effect.get("status") != "succeeded" or effect.get("simulation") is not True or effect.get("provider_reference") != "SIM-" + key:
            raise ValueError("Effect provider evidence is invalid.")
        if effect.get("amount_cents") != state["orders"][identifier]["total_cents"]:
            raise ValueError("Effect amount does not agree with order total.")
        if kind in ("shipment", "refund") and f"{identifier}:payment" not in state["effects"]:
            raise ValueError("Shipment or refund has no captured payment.")
        if kind == "refund" and state["orders"][identifier].get("payment_status") != "refunded":
            raise ValueError("Refund does not agree with the order.")
        valid_time(effect.get("at"))
    identities = []
    for key, record in state["idempotency"].items():
        if not isinstance(record, dict) or record.get("order_id") not in state["orders"]:
            raise ValueError("Idempotency order reference is invalid.")
        if not isinstance(key, str) or re.fullmatch(r"[a-f0-9]{64}", key) is None or not isinstance(record.get("payload_hash"), str) or re.fullmatch(r"[a-f0-9]{64}", record["payload_hash"]) is None:
            raise ValueError("Idempotency identity or payload fingerprint is invalid.")
        identities.append(record["order_id"])
    if len(identities) != len(set(identities)) or set(identities) != set(state["orders"]):
        raise ValueError("Every order needs one retained idempotency record.")
    for key, entry in state["outbox"].items():
        if not isinstance(entry, dict) or entry.get("id") != key or entry.get("order_id") not in state["orders"]:
            raise ValueError("Outbox identity or order reference is invalid.")
        if entry.get("type") != "work.available" or entry.get("stage") not in ("payment", "fulfillment") or entry.get("status") not in ("pending", "sent", "discarded"):
            raise ValueError("Outbox work event is invalid.")
        valid_time(entry.get("created_at"))
        valid_time(entry.get("updated_at"))
    for key, event in state["events"].items():
        valid_event(key, event)
    for key, drill in state["drills"].items():
        if not isinstance(drill, dict) or drill.get("id") != key or drill.get("status") not in ("running", "passed", "failed"):
            raise ValueError("Drill identity or status is invalid.")
        if not isinstance(drill.get("checks"), list) or not isinstance(drill.get("steps"), list) or not isinstance(drill.get("passed"), bool):
            raise ValueError("Drill evidence is invalid.")
        valid_time(drill.get("at"))


class Engine:
    def __init__(self, repository):
        self.repository = repository
        if repository.mode == "local_demo":
            repository.initialize(initial_state())

    @property
    def mode(self):
        return self.repository.mode

    def submit(self, payload, actor="operator"):
        if not isinstance(payload, dict) or set(payload) - {"customer", "items", "scenario", "idempotency_key"}:
            raise OrderFlowError(400, "invalid_input", "Use customer, items, scenario and idempotency_key fields.")
        customer = payload.get("customer")
        if not isinstance(customer, str) or not 1 <= len(customer.strip()) <= 80 or any(ord(c) < 32 for c in customer):
            raise OrderFlowError(400, "invalid_input", "Customer name must contain 1–80 printable characters.")
        key = payload.get("idempotency_key")
        if not isinstance(key, str) or KEY_PATTERN.fullmatch(key) is None:
            raise OrderFlowError(400, "invalid_input", "Provide an idempotency key of 8–128 letters, digits, dashes or underscores.")
        if not isinstance(actor, str) or not 1 <= len(actor) <= 256:
            raise ValueError("An actor identity is required.")
        identity_key = sha256((actor + "\0" + key).encode()).hexdigest()
        scenario = payload.get("scenario", "happy_path")
        if scenario not in SCENARIOS:
            raise OrderFlowError(400, "invalid_input", "Choose a supported simulation scenario.")
        supplied = payload.get("items")
        if not isinstance(supplied, list) or not 1 <= len(supplied) <= 10:
            raise OrderFlowError(400, "invalid_input", "An order needs 1–10 distinct products.")
        quantities = {}
        for item in supplied:
            if not isinstance(item, dict) or set(item) != {"sku", "quantity"} or not isinstance(item.get("sku"), str):
                raise OrderFlowError(400, "invalid_input", "Each item needs a product SKU and quantity.")
            if item["sku"] in quantities:
                raise OrderFlowError(400, "invalid_input", "Combine repeated products into a single item.")
            quantities[item["sku"]] = _integer(item["quantity"], "Item quantity", maximum=100)
        normalized = {"customer": customer.strip(), "scenario": scenario, "items": sorted(quantities.items())}
        fingerprint = sha256(json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        identifier, at = uuid.uuid4().hex, timestamp()

        def create(state):
            previous = state["idempotency"].get(identity_key)
            if previous:
                if previous["payload_hash"] != fingerprint:
                    raise Conflict("idempotency_conflict", "This request key already belongs to a different order. Use a new key.")
                return _copy(_order(state, previous["order_id"])), True
            if len(state["orders"]) >= MAX_ORDERS:
                raise Conflict("workspace_limit", "This bounded demonstration workspace holds 250 orders. Start a fresh local database for a new exercise.")
            items = []
            for sku, quantity in sorted(quantities.items()):
                product = state["inventory"].get(sku)
                if not product:
                    raise OrderFlowError(400, "unknown_product", f"Unknown product: {sku}.")
                if product["on_hand"] - product["reserved"] < quantity:
                    raise Conflict("insufficient_stock", f"There is not enough available stock for {product['name']}.")
                items.append({"sku": sku, "name": product["name"], "quantity": quantity, "price_cents": product["price_cents"]})
            for item in items:
                state["inventory"][item["sku"]]["reserved"] += item["quantity"]
            order = {"id": identifier, "reference": "OF-" + identifier[:8].upper(), "customer": normalized["customer"],
                     "items": items, "total_cents": sum(i["price_cents"] * i["quantity"] for i in items),
                     "status": "queued", "stage": "payment", "payment_status": "pending", "shipment_status": "pending",
                     "scenario": scenario, "reservation_active": True, "created_at": at, "updated_at": at,
                     "last_error": None, "timeline": [], "simulation": True, "created_by": actor}
            state["orders"][identifier] = order
            state["idempotency"][identity_key] = {"order_id": identifier, "payload_hash": fingerprint}
            state["work"][identifier] = {"id": "work-" + identifier, "order_id": identifier, "stage": "payment",
                                        "status": "pending", "attempts": 0, "max_attempts": 3,
                                        "last_error": None, "created_at": at, "updated_at": at}
            _event(state, order, "order.accepted", "Order accepted", "Stock reserved atomically; payment is waiting for the worker.", actor, at)
            _enqueue(state, order, "payment", at)
            return _copy(order), False
        return self.repository.transact(create)

    def catalog(self):
        state = self.repository.load()
        return {"products": [_product(v) for v in state["inventory"].values()], "currency": "USD", "simulation": True}

    def get_order(self, identifier):
        return _copy(_order(self.repository.load(), identifier))

    def orders(self, status=None, search=""):
        if status and status not in ("queued", "processing", "completed", "cancelled", "failed"):
            raise OrderFlowError(400, "invalid_input", "Unknown order status filter.")
        if not isinstance(search, str) or len(search) > 80:
            raise OrderFlowError(400, "invalid_input", "Search must be at most 80 characters.")
        orders = self.repository.load()["orders"].values()
        result = [o for o in orders if (not status or o["status"] == status)
                  and (not search or search.casefold() in (o["customer"] + " " + o["reference"]).casefold())]
        result.sort(key=lambda o: o["created_at"], reverse=True)
        return {"orders": result, "count": len(result)}

    @staticmethod
    def _metrics(state):
        orders, work = list(state["orders"].values()), list(state["work"].values())
        return {"total_orders": len(orders), "completed_orders": sum(o["status"] == "completed" for o in orders),
                "pending_orders": sum(o["status"] in ("queued", "processing") for o in orders),
                "failed_orders": sum(o["status"] == "failed" for o in orders),
                "revenue_cents": sum(o["total_cents"] for o in orders if o["status"] == "completed"),
                "queue_depth": sum(w["status"] == "pending" for w in work),
                "dead_letters": sum(w["status"] == "dead_letter" for w in work),
                "reserved_units": sum(p["reserved"] for p in state["inventory"].values())}

    def dashboard(self):
        state = self.repository.load()
        return {"metrics": self._metrics(state), "recent_orders": sorted(state["orders"].values(), key=lambda o: o["created_at"], reverse=True)[:8],
                "activity": sorted(state["events"].values(), key=lambda e: e["at"], reverse=True)[:12],
                "mode": self.mode, "simulation": True}

    def operations(self):
        state = self.repository.load()
        pending = sorted((w for w in state["work"].values() if w["status"] == "pending"), key=lambda w: w["created_at"])
        blocked = sorted((w for w in state["work"].values() if w["status"] == "dead_letter"), key=lambda w: w["updated_at"], reverse=True)
        return {"queue": pending, "dead_letters": blocked, "metrics": self._metrics(state),
                "activity": sorted(state["events"].values(), key=lambda e: e["at"], reverse=True)[:50],
                "drills": sorted(state["drills"].values(), key=lambda d: d["at"], reverse=True),
                "mode": self.mode, "outbox_pending": sum(e["status"] == "pending" for e in state["outbox"].values()),
                "limits": {"orders": MAX_ORDERS, "events": MAX_EVENTS, "attempts": 3}, "simulation": True,
                "queue_note": "Recorded workflow state; AWS queue metrics may lag this view."}

    def process_order(self, identifier, actor="worker", expected_stage=None, crash_after_effect=False):
        at = timestamp()

        def advance(state):
            order = _order(state, identifier)
            work = state["work"][identifier]
            if expected_stage is not None and work["stage"] != expected_stage:
                return {"outcome": "ignored", "order_id": identifier}
            if work["status"] == "dead_letter":
                return {"outcome": "dead_letter", "order_id": identifier, "error": work["last_error"]}
            if work["status"] != "pending":
                return {"outcome": "ignored", "order_id": identifier}
            stage = work["stage"]
            work["updated_at"] = at
            # The local publisher acknowledges only the stage it actually delivers.
            if self.mode == "local_demo":
                for entry in state["outbox"].values():
                    if entry["order_id"] == identifier and entry["stage"] == stage and entry["status"] == "pending":
                        entry.update(status="sent", updated_at=at)
            if stage == "payment" and order["scenario"] == "payment_declined":
                _release(state, order)
                order.update(status="failed", stage="done", payment_status="declined", last_error="Simulated payment was declined.")
                work.update(status="done", stage="done", last_error=order["last_error"])
                _event(state, order, "payment.declined", "Payment declined", "The simulation declined payment. Reserved stock was released.", actor, at)
                return {"outcome": "completed", "order_id": identifier, "status": "failed"}
            effect_key = f"{identifier}:{'payment' if stage == 'payment' else 'shipment'}"
            exists = effect_key in state["effects"]
            transient = stage == "fulfillment" and not exists and (order["scenario"] == "fulfillment_dead_letter"
                        or (order["scenario"] == "fulfillment_retry" and work["attempts"] == 0))
            if transient:
                work["attempts"] += 1
                error = "Simulated fulfillment provider is unavailable."
                work["last_error"] = order["last_error"] = error
                terminal = work["attempts"] >= work["max_attempts"]
                if terminal:
                    work["status"] = "dead_letter"
                    order["status"] = "failed"
                _event(state, order, "work.blocked" if terminal else "work.retry", "Recovery required" if terminal else "Retry scheduled",
                       f"{error} Attempt {work['attempts']} of {work['max_attempts']}. Stock stays reserved until recovery or cancellation.", actor, at)
                return {"outcome": "dead_letter" if terminal else "retry", "order_id": identifier, "error": error}
            if not exists:
                state["effects"][effect_key] = {"id": effect_key, "order_id": identifier,
                    "type": "payment" if stage == "payment" else "shipment", "status": "succeeded", "amount_cents": order["total_cents"],
                    "provider_reference": "SIM-" + effect_key, "at": at, "simulation": True}
                _event(state, order, f"{stage}.effect", "Simulated payment captured" if stage == "payment" else "Simulated shipment created",
                       "One persisted effect record, protected against repeated delivery.", actor, at)
            if crash_after_effect:
                _event(state, order, "worker.interrupted", "Worker interrupted", "Drill interruption after effect commit and before the next stage; redelivery will reuse the effect.", actor, at)
                return {"outcome": "retry", "order_id": identifier, "error": "Injected interruption after effect."}
            order["last_error"] = work["last_error"] = None
            if stage == "payment":
                order.update(status="processing", stage="fulfillment", payment_status="captured")
                work.update(stage="fulfillment", attempts=0)
                _event(state, order, "payment.confirmed", "Ready for fulfillment", "Payment is simulated; the reservation remains active.", actor, at)
                _enqueue(state, order, "fulfillment", at)
                return {"outcome": "advanced", "order_id": identifier}
            if stage != "fulfillment":
                raise Conflict("invalid_stage", "Work has an unknown stage.")
            if not order["reservation_active"]:
                raise Conflict("missing_reservation", "Fulfillment requires an active inventory reservation.")
            for item in order["items"]:
                state["inventory"][item["sku"]]["on_hand"] -= item["quantity"]
            _release(state, order)
            order.update(status="completed", stage="done", shipment_status="shipped")
            work.update(status="done", stage="done")
            _event(state, order, "order.completed", "Order completed", "Simulated shipment confirmed. Reserved units were deducted once.", actor, at)
            return {"outcome": "completed", "order_id": identifier, "status": "completed"}
        return self.repository.transact(advance)

    def process(self, limit=20, actor="worker"):
        _integer(limit, "Processing limit", maximum=20)
        results = []
        # One pass through each pending stage avoids tight-looping a retryable failure.
        pending = sorted((w for w in self.repository.load()["work"].values() if w["status"] == "pending"), key=lambda w: w["created_at"])
        for work in pending[:limit]:
            results.append(self.process_order(work["order_id"], actor, expected_stage=work["stage"]))
        return {"processed": len(results), "results": results}

    def retry(self, identifier, actor="operator"):
        at = timestamp()
        def retry(state):
            order = _order(state, identifier)
            work = state["work"][identifier]
            if work["status"] != "dead_letter" or not order["reservation_active"]:
                raise Conflict("not_recoverable", "Only blocked fulfillment work with an active reservation can be retried.")
            order.update(status="processing", scenario="happy_path", last_error=None)
            work.update(status="pending", attempts=0, last_error=None, updated_at=at)
            _event(state, order, "work.redriven", "Recovery started", "The simulated provider is healthy. Resume fulfillment using the existing payment and reservation.", actor, at)
            _enqueue(state, order, work["stage"], at)
            return _copy(order)
        return self.repository.transact(retry)

    def cancel(self, identifier, actor="operator"):
        at = timestamp()
        def cancel(state):
            order = _order(state, identifier)
            if order["status"] == "cancelled":
                return _copy(order)
            if order["status"] == "completed" or f"{identifier}:shipment" in state["effects"]:
                raise Conflict("already_shipped", "This shipment is already committed and requires a separate returns workflow.")
            _release(state, order)
            if f"{identifier}:payment" in state["effects"]:
                key = f"{identifier}:refund"
                if key not in state["effects"]:
                    state["effects"][key] = {"id": key, "order_id": identifier, "type": "refund", "status": "succeeded",
                                              "amount_cents": order["total_cents"], "provider_reference": "SIM-" + key, "at": at, "simulation": True}
                order["payment_status"] = "refunded"
            elif order["payment_status"] == "pending":
                order["payment_status"] = "cancelled"
            order.update(status="cancelled", stage="done", shipment_status="cancelled", last_error=None)
            state["work"][identifier].update(status="done", stage="done", last_error=None, updated_at=at)
            for entry in state["outbox"].values():
                if entry["order_id"] == identifier and entry["status"] == "pending":
                    entry.update(status="discarded", updated_at=at)
            _event(state, order, "order.cancelled", "Order cancelled", "Stock released once. Any captured simulated payment was refunded once.", actor, at)
            return _copy(order)
        return self.repository.transact(cancel)

    def restock(self, sku, quantity, actor="admin"):
        _integer(quantity, "Restock quantity", maximum=1000)
        at = timestamp()
        def restock(state):
            product = state["inventory"].get(sku)
            if product is None:
                raise NotFound("That product does not exist.")
            if product["on_hand"] + quantity > 100000:
                raise Conflict("inventory_limit", "This demonstration caps inventory at 100,000 units per product.")
            product["on_hand"] += quantity
            _event(state, None, "inventory.adjusted", "Inventory received", f"Added {quantity} units to {sku}.", actor, at)
            return _product(product)
        return self.repository.transact(restock)

    def run_drill(self, actor="admin", drill_id=None):
        from .drills import recovery_drill
        proof = recovery_drill(drill_id)
        at = proof["at"]
        def record(state):
            state["drills"][proof["id"]] = proof
            while len(state["drills"]) > MAX_DRILLS:
                oldest = min(state["drills"], key=lambda key: state["drills"][key]["at"])
                del state["drills"][oldest]
            _event(state, None, "drill.completed", "Recovery drill " + proof["status"], "Isolated test data; working orders and inventory were preserved.", actor, at)
            return _copy(proof)
        return self.repository.transact(record)

    def seed_demo(self):
        if self.mode != "local_demo":
            raise Conflict("local_only", "Sample orders are only seeded in the local demonstration.")
        if self.repository.load()["orders"]:
            return {"seeded": False, "reason": "Orders already exist; no existing records were replaced."}
        samples = [("Harper Studio", "TOTE-001", 2, "happy_path"), ("Northline Design", "LAMP-002", 1, "happy_path"),
                   ("River & Co.", "MUG-004", 3, "fulfillment_dead_letter"), ("Olive Workshop", "NOTE-003", 4, "fulfillment_retry"),
                   ("Atlas Supply", "AUDIO-005", 1, "payment_declined"), ("Finch House", "CHAIR-006", 1, "happy_path")]
        for index, (customer, sku, quantity, scenario) in enumerate(samples):
            self.submit({"customer": customer, "items": [{"sku": sku, "quantity": quantity}], "scenario": scenario,
                         "idempotency_key": f"seed-order-{index:04}"}, "demo-seed")
        for _ in range(4):
            self.process()
        return {"seeded": True, "orders": len(samples)}
