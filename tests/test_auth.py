"""Authorization is enforced at adapters and the shared API, before data access."""
from http.client import HTTPMessage
import io
import json
import os
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from aws.handlers import api as cloud_api
from retail_forecast.api import handle_request
from retail_forecast.auth import (COOKIE_NAME, Principal, SessionStore, anonymous_session,
                                 cognito_config, local_config, request_origin,
                                 verified_cognito_principal)
from retail_forecast.server import Handler, create_server, serve


VIEWER = Principal("fixture-viewer", "viewer", "Viewer fixture", "local_demo")
PLANNER = Principal("fixture-planner", "planner", "Planner fixture", "local_demo")
AUTH_ENV = {"AUTH_ENABLED": "true", "AUTH_ISSUER": "https://cognito-idp.ap-southeast-2.amazonaws.com/test-pool",
            "AUTH_CLIENT_ID": "test-public-client", "AUTH_DOMAIN": "https://test.auth.ap-southeast-2.amazoncognito.com",
            "AUTH_CALLBACK_URL": "https://test.example/", "AUTH_LOGOUT_URL": "https://test.example/",
            "AUTH_SCOPES": "openid profile retail/read retail/plan", "RESULTS_TABLE": "test-results"}


def cloud_event(method="GET", path="/api/catalog", claims=None):
    event = {"requestContext": {"http": {"method": method}}, "rawPath": path}
    if claims is not None:
        event["requestContext"]["authorizer"] = {"jwt": {"claims": claims}}
    return event


def access_claims(role="viewer", now=1_800_000_000):
    return {"token_use": "access", "iss": AUTH_ENV["AUTH_ISSUER"], "client_id": AUTH_ENV["AUTH_CLIENT_ID"],
            "sub": "fixture-subject", "exp": str(now + 3600), "iat": str(now - 30),
            "scope": "openid retail/read retail/plan", "cognito:groups": json.dumps([role])}


def repository():
    repo = Mock(mode="local")
    repo.catalog.return_value = {"default_cutoff": 224, "total_days": 252,
        "products": [{"store_id": "CA_1", "item_id": "FOODS_1_001"}],
        "models": [{"id": "seasonal", "available": True}]}
    repo.forecast.return_value = {"source": "synthetic", "source_label": "Generated auth fixture",
        "as_of": "2020-08-11", "model": "seasonal", "_actuals": [10] * 28,
        "forecast": [{"p10": 8, "p50": 10, "p90": 12, "baseline": 10}] * 28}
    repo.operations.return_value = {"mode": "local"}
    repo.releases.return_value = {"releases": []}
    repo.recovery_drill.return_value = {"latest_drill": {"status": "completed"}}
    repo.demo_releases.return_value = {"releases": []}
    return repo


class SharedAuthorizationTests(unittest.TestCase):
    def test_anonymous_cannot_read_or_write_repository(self):
        repo = repository()
        for method, path in [("GET", "/api/catalog"), ("GET", "/api/forecast"), ("GET", "/api/releases"),
                             ("GET", "/api/operations"), ("POST", "/api/simulate"),
                             ("POST", "/api/operations/drill"), ("POST", "/api/releases/demo")]:
            self.assertEqual(handle_request(method, path, {}, {}, repo)[0], 401)
        self.assertEqual(repo.mock_calls, [])
        # Plain client data is not an internally verified Principal.
        self.assertEqual(handle_request("GET", "/api/catalog", {}, {}, repo, {"role": "planner"})[0], 401)

    def test_public_health_config_and_anonymous_session(self):
        repo = repository()
        self.assertEqual(handle_request("GET", "/api/health", {}, {}, repo)[0], 200)
        self.assertEqual(handle_request("GET", "/api/auth/config", {}, {}, repo, auth_config=local_config())[1]["mode"], "local_demo")
        self.assertEqual(handle_request("GET", "/api/auth/session", {}, {}, repo)[1], anonymous_session())
        self.assertEqual(repo.mock_calls, [])

    def test_viewer_reads_all_views_but_cannot_run_actions(self):
        repo = repository()
        selection = {"store": "CA_1", "item": "FOODS_1_001"}
        for path in ("/api/catalog", "/api/forecast", "/api/releases", "/api/operations"):
            self.assertEqual(handle_request("GET", path, selection, {}, repo, VIEWER)[0], 200)
        repo.reset_mock()
        for path in ("/api/simulate", "/api/operations/drill", "/api/releases/demo"):
            self.assertEqual(handle_request("POST", path, {}, {}, repo, VIEWER)[0], 403)
        self.assertEqual(repo.mock_calls, [])
        self.assertFalse(VIEWER.session()["permissions"]["can_simulate"])

    def test_planner_runs_simulation_and_local_workflow_actions(self):
        repo = repository()
        selection = {"store": "CA_1", "item": "FOODS_1_001"}
        for path, body in (("/api/simulate", selection), ("/api/operations/drill", {}), ("/api/releases/demo", {})):
            self.assertEqual(handle_request("POST", path, {}, body, repo, PLANNER)[0], 200)
        repo.recovery_drill.assert_called_once_with(None)
        repo.demo_releases.assert_called_once()

    def test_expired_internal_principal_cannot_access_data(self):
        expired = Principal("expired", "planner", "Expired fixture", "cognito", 1)
        repo = repository()
        self.assertEqual(handle_request("GET", "/api/catalog", {}, {}, repo, expired)[0], 401)
        repo.catalog.assert_not_called()


class SessionTests(unittest.TestCase):
    def test_opaque_sessions_expire_revoke_and_never_decode_client_roles(self):
        now = [10.0]
        store = SessionStore(clock=lambda: now[0])
        token, session = store.create("viewer")
        self.assertEqual(len(token), 43)
        self.assertNotIn(token, json.dumps(session))
        self.assertTrue(store.valid_csrf(token, session["csrf_token"]))
        self.assertFalse(store.valid_csrf(token, "forged"))
        self.assertIsNone(store.get(json.dumps({"role": "planner"})))
        self.assertIsNone(store.get("a" * 43))
        now[0] += 3600
        self.assertIsNone(store.get(token))
        new, _ = store.create("planner")
        store.delete(new)
        self.assertIsNone(store.get(new))

    def test_role_changes_rotate_the_session_and_store_is_bounded(self):
        store = SessionStore(maximum=2)
        old, _ = store.create("viewer")
        new, _ = store.create("planner", old)
        self.assertIsNone(store.get(old))
        self.assertEqual(store.get(new)["principal"].role, "planner")
        store.create("viewer")
        store.create("viewer")
        self.assertIsNone(store.get(new))
        self.assertEqual(len(store._sessions), 2)
        for role in ("admin", None, True):
            with self.assertRaises(ValueError):
                store.create(role)

    def test_origin_and_loopback_binding_restrictions(self):
        self.assertTrue(request_origin("127.0.0.1:8000", "http://127.0.0.1:8000", "same-origin", 8000, True))
        for host, origin, site in [("evil.example:8000", "http://evil.example:8000", "same-origin"),
                                   ("localhost:8000", "http://evil.example", "cross-site"),
                                   ("localhost:8000", "null", "cross-site"),
                                   ("localhost:8000", None, "same-origin"),
                                   ("localhost:8000", "http://localhost:8001", "same-origin"),
                                   ("localhost:8000", "http://localhost:8000", "same-site")]:
            self.assertFalse(request_origin(host, origin, site, 8000, True))
        with patch("retail_forecast.server.LocalRepository") as repo:
            for host in ("0.0.0.0", "192.168.1.2", "::", "public.example"):
                with self.assertRaisesRegex(ValueError, "loopback"):
                    serve(host=host)
                with self.assertRaises(ValueError):
                    create_server(host, 8000, repository())
            repo.assert_not_called()


class LocalAdapterTests(unittest.TestCase):
    def setUp(self):
        self.repo = repository()
        self.sessions = SessionStore()

    def request(self, method, path, body=None, cookie=None, csrf=None, origin="http://127.0.0.1:8000", host="127.0.0.1:8000", extra=None):
        handler = Handler.__new__(Handler)
        handler.command, handler.path = method, path
        handler.server = SimpleNamespace(server_port=8000, sessions=self.sessions, repository=self.repo)
        raw = json.dumps(body if body is not None else {}).encode()
        handler.headers = HTTPMessage()
        for key, value in {"Host": host, "Content-Type": "application/json", "Content-Length": str(len(raw)),
                           **({"Origin": origin} if origin is not None else {}),
                           **({"Cookie": f"{COOKIE_NAME}={cookie}"} if cookie else {}),
                           **({"X-CSRF-Token": csrf} if csrf else {}), **(extra or {})}.items():
            handler.headers[key] = value
        handler.rfile = io.BytesIO(raw)
        handler._json = Mock()
        handler._api()
        return handler._json.call_args.args

    def login(self, role="planner"):
        status, session, cookie = self.request("POST", "/api/auth/login", {"role": role})
        self.assertEqual(status, 200)
        for attribute in ("HttpOnly", "SameSite=Strict", "Path=/", "Max-Age=3600"):
            self.assertIn(attribute, cookie)
        token = cookie.split(";", 1)[0].split("=", 1)[1]
        return token, session

    def test_login_session_and_logout_require_origin_cookie_and_csrf(self):
        token, session = self.login()
        status, result = self.request("GET", "/api/auth/session", cookie=token, origin=None)
        self.assertEqual(status, 200)
        self.assertEqual(result["role"], "planner")
        self.assertEqual(result["csrf_token"], session["csrf_token"])
        self.assertGreater(result["expires_in"], 0)
        self.assertEqual(self.request("POST", "/api/auth/logout", cookie=token)[0], 403)
        self.assertIsNotNone(self.sessions.get(token))
        status, result, cookie = self.request("POST", "/api/auth/logout", cookie=token, csrf=session["csrf_token"])
        self.assertEqual(status, 200)
        self.assertFalse(result["authenticated"])
        self.assertIn("Max-Age=0", cookie)
        self.assertIsNone(self.sessions.get(token))
        self.assertEqual(self.request("GET", "/api/catalog", cookie=token, origin=None)[0], 401)

    def test_invalid_login_or_cross_origin_request_does_not_create_a_session(self):
        for body in ({"role": "admin"}, {"role": "planner", "password": "unused"}, {}, []):
            self.assertEqual(self.request("POST", "/api/auth/login", body)[0], 400)
        for origin in (None, "null", "http://evil.example"):
            self.assertEqual(self.request("POST", "/api/auth/login", {"role": "planner"}, origin=origin)[0], 403)
        self.assertEqual(self.request("POST", "/api/auth/login", {"role": "planner"}, extra={"Sec-Fetch-Site": "cross-site"})[0], 403)
        self.assertEqual(len(self.sessions._sessions), 0)

    def test_viewer_denied_and_planner_mutations_require_csrf(self):
        viewer, viewer_session = self.login("viewer")
        for path in ("/api/simulate", "/api/operations/drill", "/api/releases/demo"):
            self.assertEqual(self.request("POST", path, cookie=viewer, csrf=viewer_session["csrf_token"])[0], 403)
        planner, planner_session = self.login("planner")
        for csrf in (None, "bad", "\u00e9" * 43, viewer_session["csrf_token"]):
            self.assertEqual(self.request("POST", "/api/operations/drill", cookie=planner, csrf=csrf)[0], 403)
        self.assertEqual(self.request("POST", "/api/operations/drill", cookie=planner,
                                      csrf=planner_session["csrf_token"], origin="http://evil.example")[0], 403)
        self.repo.recovery_drill.assert_not_called()
        self.assertEqual(self.request("POST", "/api/operations/drill", cookie=planner, csrf=planner_session["csrf_token"])[0], 200)
        self.repo.recovery_drill.assert_called_once()

    def test_forged_cookie_or_role_header_does_not_grant_identity(self):
        self.assertEqual(self.request("GET", "/api/catalog", cookie="a" * 43, origin=None,
            extra={"X-Role": "planner", "Authorization": "Bearer unsigned.client.claims"})[0], 401)
        self.assertEqual(self.repo.mock_calls, [])

    def test_expired_local_session_becomes_anonymous_and_cannot_mutate(self):
        now = [0.0]
        self.sessions = SessionStore(clock=lambda: now[0])
        token, session = self.login()
        now[0] = 3500
        self.assertEqual(self.request("GET", "/api/auth/session", cookie=token, origin=None)[1]["expires_in"], 100)
        now[0] = 3600
        self.assertFalse(self.request("GET", "/api/auth/session", cookie=token, origin=None)[1]["authenticated"])
        self.assertEqual(self.request("POST", "/api/operations/drill", cookie=token, csrf=session["csrf_token"])[0], 401)
        self.repo.recovery_drill.assert_not_called()


class CognitoAuthorizationTests(unittest.TestCase):
    def test_verified_claims_accept_exact_roles_and_gateway_array_formats(self):
        for groups in (["viewer"], '["viewer"]', "viewer", "[viewer]", "[viewer, planner]"):
            claims = {**access_claims(), "cognito:groups": groups}
            principal = verified_cognito_principal(cloud_event(claims=claims), AUTH_ENV, 1_800_000_000)
            self.assertEqual(principal.role, "planner" if "planner" in str(groups) else "viewer")
        self.assertIsNone(verified_cognito_principal(cloud_event(), AUTH_ENV, 1_800_000_000))

    def test_access_token_claims_fail_closed(self):
        for field, value in [("token_use", "id"), ("iss", "https://wrong.example"), ("client_id", "wrong-client"),
                             ("exp", "NaN"), ("exp", True), ("exp", "1800000000"), ("iat", "Infinity"),
                             ("iat", "1800000100"), ("iat", "1800010000"), ("scope", "openid retail/plan"),
                             ("sub", ""), ("sub", ["subject"])]:
            with self.subTest(field=field, value=value):
                self.assertIsNone(verified_cognito_principal(cloud_event(claims={**access_claims(), field: value}), AUTH_ENV, 1_800_000_000))
        for field in ("exp", "iat", "iss", "sub", "client_id", "token_use", "scope"):
            claims = access_claims()
            claims.pop(field)
            self.assertIsNone(verified_cognito_principal(cloud_event(claims=claims), AUTH_ENV, 1_800_000_000))
        self.assertIsNone(verified_cognito_principal(cloud_event(claims=access_claims()), {**AUTH_ENV, "AUTH_ENABLED": "false"}, 1_800_000_000))

    def test_valid_token_without_group_has_no_application_access(self):
        for groups in (None, [], "admin", '["planners"]', {"role": "planner"}):
            with self.assertRaisesRegex(PermissionError, "not assigned"):
                verified_cognito_principal(cloud_event(claims={**access_claims(), "cognito:groups": groups}), AUTH_ENV, 1_800_000_000)

    def test_cloud_headers_and_unsigned_tokens_never_establish_identity(self):
        with patch.dict(os.environ, AUTH_ENV, clear=True), patch.object(cloud_api, "DynamoRepository") as repo:
            event = cloud_event()
            event["headers"] = {"Authorization": "Bearer unsigned.claims", "X-Role": "planner"}
            event["body"] = json.dumps({"role": "planner"})
            self.assertEqual(cloud_api.handler(event, None)["statusCode"], 401)
            repo.assert_not_called()

    def test_cloud_public_config_and_role_session_never_call_aws(self):
        with patch.dict(os.environ, {**AUTH_ENV, "PRIVATE_SECRET": "hidden"}, clear=True), patch.object(cloud_api, "DynamoRepository") as repo:
            for path in ("/api/health", "/api/auth/config"):
                result = cloud_api.handler(cloud_event(path=path), None)
                self.assertEqual(result["statusCode"], 200)
                self.assertNotIn("hidden", result["body"])
            claims = access_claims(now=int(time.time()))
            result = cloud_api.handler(cloud_event(path="/api/auth/session", claims=claims), None)
            self.assertEqual(result["statusCode"], 200)
            self.assertEqual(json.loads(result["body"])["role"], "viewer")
            self.assertNotIn("csrf_token", result["body"])
            self.assertNotIn("fixture-subject", result["body"])
            repo.assert_not_called()

    def test_cloud_viewer_cannot_mutate_and_unassigned_user_returns_403(self):
        with patch.dict(os.environ, AUTH_ENV, clear=True), patch.object(cloud_api, "DynamoRepository") as repo:
            claims = access_claims(now=int(time.time()))
            for path in ("/api/simulate", "/api/operations/drill", "/api/releases/demo"):
                result = cloud_api.handler(cloud_event("POST", path, claims), None)
                self.assertEqual(result["statusCode"], 403)
            claims.pop("cognito:groups")
            self.assertEqual(cloud_api.handler(cloud_event(path="/api/auth/session", claims=claims), None)["statusCode"], 403)
            repo.assert_not_called()

    def test_cloud_verified_viewer_reads_forecast_and_planner_simulates(self):
        repo = repository()
        with patch.dict(os.environ, AUTH_ENV, clear=True), patch.object(cloud_api, "DynamoRepository", return_value=repo) as factory:
            selection = {"store": "CA_1", "item": "FOODS_1_001"}
            claims = access_claims(now=int(time.time()))
            event = cloud_event(path="/api/forecast", claims=claims)
            event["queryStringParameters"] = selection
            result = cloud_api.handler(event, None)
            self.assertEqual(result["statusCode"], 200)
            self.assertNotIn("_actuals", result["body"])
            claims["cognito:groups"] = '["planner"]'
            event = cloud_event("POST", "/api/simulate", claims)
            event["body"] = json.dumps(selection)
            result = cloud_api.handler(event, None)
            self.assertEqual(result["statusCode"], 200)
            self.assertEqual(len(json.loads(result["body"])["policies"]), 3)
            self.assertEqual(factory.call_count, 2)


if __name__ == "__main__":
    unittest.main()
