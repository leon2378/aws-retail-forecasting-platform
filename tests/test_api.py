from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from orderflow.api import handle_request
from orderflow.auth import Principal
from orderflow.engine import Engine
from orderflow.storage import SQLiteRepository


class ApiTests(unittest.TestCase):
    def setUp(self):
        Path("build").mkdir(exist_ok=True)
        self.directory = TemporaryDirectory(dir="build")
        self.engine = Engine(SQLiteRepository(Path(self.directory.name) / "workspace.db"))
        self.admin = Principal("test-admin", "admin", "Admin", "local_demo")

    def tearDown(self):
        self.directory.cleanup()

    def request(self, method, path, body=None, principal=None):
        return handle_request(method, path, {}, body or {}, self.engine, principal)

    def test_health_and_config_public_but_workspace_private(self):
        self.assertEqual(self.request("GET", "/api/health")[0], 200)
        self.assertEqual(self.request("GET", "/api/auth/config")[1]["roles"], ["viewer", "operator", "admin"])
        self.assertEqual(self.request("GET", "/api/orders")[0], 401)

    def test_schema_unknown_fields_and_boolean_quantities_rejected(self):
        payload = {"customer": "Test", "items": [{"sku": "TOTE-001", "quantity": True}], "idempotency_key": "test-key-123"}
        self.assertEqual(self.request("POST", "/api/orders", payload, self.admin)[0], 400)
        payload["items"][0]["quantity"] = 1
        payload["privileged"] = True
        self.assertEqual(self.request("POST", "/api/orders", payload, self.admin)[0], 400)
        self.assertEqual(self.engine.dashboard()["metrics"]["total_orders"], 0)

    def test_missing_records_and_unknown_routes_have_safe_404(self):
        for path in ("/api/orders/not-found", "/api/does-not-exist", "/api/orders/a/b"):
            status, payload = self.request("GET", path, principal=self.admin)
            self.assertEqual(status, 404)
            self.assertEqual(payload["code"], "not_found")

    def test_readonly_viewer_cannot_submit_or_process(self):
        viewer = Principal("v", "viewer", "Viewer", "local_demo")
        for path in ("/api/orders", "/api/operations/process", "/api/operations/retry/a", "/api/orders/a/cancel"):
            self.assertEqual(self.request("POST", path, principal=viewer)[0], 403)

    def test_drill_isolates_existing_orders_and_reservations(self):
        payload = {"customer": "Untouched", "items": [{"sku": "NOTE-003", "quantity": 3}], "idempotency_key": "untouched-order"}
        status, result = self.request("POST", "/api/orders", payload, self.admin)
        self.assertEqual(status, 201)
        order = result["order"]
        inventory = self.engine.catalog()
        status, result = self.request("POST", "/api/operations/drill", principal=self.admin)
        self.assertEqual(status, 200)
        self.assertTrue(result["drill"]["passed"])
        self.assertEqual(inventory, self.engine.catalog())
        self.assertEqual(order, self.engine.get_order(order["id"]))
