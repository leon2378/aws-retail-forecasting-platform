"""Black-box HTTP checks against an ephemeral loopback server."""
from contextlib import closing
from http.client import HTTPConnection
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
import json
import unittest

from orderflow.auth import SessionStore
from orderflow.engine import Engine
from orderflow.server import create_server
from orderflow.storage import SQLiteRepository


class HTTPAcceptance(unittest.TestCase):
    def setUp(self):
        scratch = Path(__file__).resolve().parents[1] / "build"
        scratch.mkdir(exist_ok=True)
        self.workspace = TemporaryDirectory(dir=scratch)
        self.repository = SQLiteRepository(Path(self.workspace.name) / "orders.db")
        self.engine = Engine(self.repository)
        frontend = Path(self.workspace.name) / "frontend"
        frontend.mkdir()
        (frontend / "index.html").write_text("<!doctype html><title>OrderFlow</title>", encoding="utf-8")
        self.server = create_server("127.0.0.1", 0, self.engine, directory=frontend)
        self.origin = f"http://127.0.0.1:{self.server.server_port}"
        self.worker = Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.cookie = None
        self.csrf = None

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=3)
        self.workspace.cleanup()

    def request(self, method, path, payload=None, *, headers=None, raw=None):
        defaults = {}
        if self.cookie:
            defaults["Cookie"] = self.cookie
        if method == "POST":
            defaults.update({"Origin": self.origin, "Content-Type": "application/json"})
            if self.csrf:
                defaults["X-CSRF-Token"] = self.csrf
            raw = json.dumps({} if payload is None else payload).encode() if raw is None else raw
        defaults.update(headers or {})
        defaults = {key: value for key, value in defaults.items() if value is not None}
        with closing(HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)) as connection:
            connection.request(method, path, body=raw, headers=defaults)
            response = connection.getresponse()
            content = response.read()
            content_type = response.getheader("Content-Type", "")
            result = json.loads(content) if content and "application/json" in content_type else content
            return response.status, dict(response.getheaders()), result

    def login(self, role="operator"):
        status, headers, payload = self.request("POST", "/api/auth/login", {"role": role})
        self.assertEqual(status, 200)
        self.cookie = headers["Set-Cookie"].split(";", 1)[0]
        self.csrf = payload["csrf_token"]
        return headers, payload

    @staticmethod
    def order_payload():
        return {"customer": "Example Studio", "items": [{"sku": "TOTE-001", "quantity": 2}],
                "scenario": "happy_path", "idempotency_key": "http-request-0001"}

    def test_public_health_and_anonymous_data_denial(self):
        status, headers, payload = self.request("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(payload["service"], "OrderFlow")
        self.assertEqual(headers["Cache-Control"], "no-store")
        for route in ("/api/catalog", "/api/orders", "/api/operations", "/api/dashboard"):
            with self.subTest(route=route):
                self.assertEqual(self.request("GET", route)[0], 401)

    def test_login_requires_same_origin_and_rejects_forged_role(self):
        self.assertEqual(self.request("POST", "/api/auth/login", {"role": "admin"},
                                      headers={"Origin": "https://untrusted.example"})[0], 403)
        self.assertEqual(self.request("POST", "/api/auth/login", {"role": "admin"},
                                      headers={"Origin": None})[0], 403)
        self.assertEqual(self.request("POST", "/api/auth/login", {"role": "root"})[0], 400)
        self.assertEqual(self.request("POST", "/api/auth/login", {"role": "viewer", "admin": True})[0], 400)

    def test_session_cookie_security_and_local_role_disclosure(self):
        headers, session = self.login("viewer")
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", headers["Set-Cookie"])
        self.assertEqual(session["role"], "viewer")
        self.assertFalse(session["permissions"]["can_create"])
        config = self.request("GET", "/api/auth/config")[2]
        self.assertEqual(config["mode"], "local_demo")
        self.assertIn("demonstration", config["notice"].lower())

    def test_viewer_cannot_mutate_or_grant_self_permissions(self):
        self.login("viewer")
        before = self.repository.load()
        routes = (("/api/orders", self.order_payload()),
                  ("/api/operations/process", {}),
                  ("/api/inventory/TOTE-001/adjust", {"quantity": 5}),
                  ("/api/operations/drill", {}))
        for route, body in routes:
            with self.subTest(route=route):
                self.assertEqual(self.request("POST", route, body, headers={"X-Role": "admin"})[0], 403)
        self.assertEqual(self.repository.load(), before)

    def test_operator_cannot_restock_or_run_admin_drill(self):
        self.login("operator")
        before = self.repository.load()
        self.assertEqual(self.request("POST", "/api/inventory/TOTE-001/adjust", {"quantity": 5})[0], 403)
        self.assertEqual(self.request("POST", "/api/operations/drill")[0], 403)
        self.assertEqual(self.repository.load(), before)

    def test_admin_restock_records_inventory_adjustment(self):
        self.login("admin")
        status, _, payload = self.request("POST", "/api/inventory/TOTE-001/adjust", {"quantity": 3})
        self.assertEqual(status, 200)
        self.assertEqual(payload["product"]["on_hand"], 51)
        self.assertTrue(any(event["type"] == "inventory.adjusted" for event in self.repository.load()["events"].values()))

    def test_csrf_and_cross_origin_denials_leave_state_unchanged(self):
        self.login()
        before = self.repository.load()
        for override in ({"X-CSRF-Token": None}, {"X-CSRF-Token": "invalid"},
                         {"Origin": "https://untrusted.example"}, {"Sec-Fetch-Site": "cross-site"}):
            with self.subTest(headers=override):
                self.assertEqual(self.request("POST", "/api/orders", self.order_payload(), headers=override)[0], 403)
        self.assertEqual(self.repository.load(), before)

    def test_logout_revokes_existing_cookie(self):
        self.login()
        self.assertEqual(self.request("POST", "/api/auth/logout")[0], 200)
        self.assertEqual(self.request("GET", "/api/catalog")[0], 401)
        self.assertFalse(self.request("GET", "/api/auth/session")[2]["authenticated"])

    def test_expired_session_has_no_application_access(self):
        current = [100.0]
        self.server.sessions = SessionStore(lifetime=2, clock=lambda: current[0])
        self.login()
        current[0] = 103.0
        self.assertEqual(self.request("GET", "/api/catalog")[0], 401)
        self.assertEqual(self.request("POST", "/api/orders", self.order_payload())[0], 401)

    def test_bad_host_cannot_access_api_or_static_document(self):
        for route in ("/", "/api/health"):
            with self.subTest(route=route):
                self.assertEqual(self.request("GET", route, headers={"Host": "untrusted.example"})[0], 403)

    def test_static_whitelist_denies_source_private_data_and_traversal(self):
        for route in ("/README.md", "/.aws/credentials", "/data/orders.db", "/../README.md",
                      "/%2e%2e/README.md", "/infra/terraform.tfvars", "/orderflow/server.py"):
            with self.subTest(route=route):
                self.assertEqual(self.request("GET", route)[0], 404)
        status, headers, document = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"OrderFlow", document)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")

    def test_malformed_non_finite_and_oversized_json_are_rejected(self):
        self.login()
        before = self.repository.load()
        for raw in (b"not json", b"[]", b'{"quantity":NaN}', b'"text"'):
            with self.subTest(raw=raw):
                self.assertEqual(self.request("POST", "/api/orders", raw=raw)[0], 400)
        self.assertEqual(self.request("POST", "/api/orders", raw=b"x" * 16385)[0], 413)
        self.assertEqual(self.request("POST", "/api/orders", self.order_payload(),
                                      headers={"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.repository.load(), before)

    def test_client_cannot_override_prices_or_request_key(self):
        self.login()
        before = self.repository.load()
        payload = self.order_payload()
        payload["items"][0]["price_cents"] = 1
        self.assertEqual(self.request("POST", "/api/orders", payload)[0], 400)
        self.assertEqual(self.request("POST", "/api/orders", self.order_payload(),
                                      headers={"Idempotency-Key": "different-request-key"})[0], 400)
        self.assertEqual(self.repository.load(), before)

    def test_http_duplicate_and_worker_steps_complete_one_order(self):
        self.login()
        first_status, _, first = self.request("POST", "/api/orders", self.order_payload())
        second_status, _, second = self.request("POST", "/api/orders", self.order_payload())
        self.assertEqual((first_status, second_status), (201, 200))
        self.assertTrue(second["replayed"])
        self.assertEqual(first["order"]["id"], second["order"]["id"])
        identifier = first["order"]["id"]
        for _ in range(2):
            self.assertEqual(self.request("POST", "/api/operations/process")[0], 200)
        status, _, payload = self.request("GET", f"/api/orders/{identifier}")
        self.assertEqual(status, 200)
        self.assertEqual(payload["order"]["status"], "completed")
        self.assertEqual(len(self.repository.load()["effects"]), 2)
        self.assertEqual(self.repository.load()["inventory"]["TOTE-001"]["on_hand"], 46)

    def test_local_server_refuses_public_bind_address(self):
        with self.assertRaises(ValueError):
            create_server("0.0.0.0", 0, self.engine)


if __name__ == "__main__":
    unittest.main()
