import time
import unittest

from orderflow.auth import Principal, SessionStore, authorize, cognito_config, verified_cognito_principal


class AuthenticationTests(unittest.TestCase):
    def setUp(self):
        self.env = {"AUTH_ISSUER": "https://cognito-idp.ap-southeast-2.amazonaws.com/example",
                    "AUTH_CLIENT_ID": "example-client", "AUTH_DOMAIN": "https://example.auth.ap-southeast-2.amazoncognito.com",
                    "AUTH_CALLBACK_URL": "https://example.invalid/", "AUTH_LOGOUT_URL": "https://example.invalid/"}
        self.now = int(time.time())
        self.claims = {"sub": "person-123", "token_use": "access", "iss": self.env["AUTH_ISSUER"],
                       "client_id": self.env["AUTH_CLIENT_ID"], "scope": "openid orderflow/read orderflow/write",
                       "exp": self.now + 900, "iat": self.now - 1, "cognito:groups": '["operator"]'}

    def verified(self, **changes):
        event = {"requestContext": {"authorizer": {"jwt": {"claims": {**self.claims, **changes}}}}}
        return verified_cognito_principal(event, self.env, self.now)

    def test_client_bearer_header_does_not_establish_identity(self):
        self.assertIsNone(verified_cognito_principal({"headers": {"authorization": "Bearer invented"}}, self.env, self.now))

    def test_verified_access_claims_and_group_produce_operator(self):
        principal = self.verified()
        self.assertEqual(principal.role, "operator")
        self.assertTrue(principal.session()["permissions"]["can_create"])
        self.assertFalse(principal.session()["permissions"]["can_drill"])

    def test_wrong_issuer_client_token_type_and_expiry_are_rejected(self):
        for changes in ({"iss": "https://attacker.invalid"}, {"client_id": "wrong"}, {"token_use": "id"},
                        {"exp": self.now}, {"iat": self.now + 120}, {"exp": "NaN"}, {"scope": "openid"}):
            with self.subTest(changes=changes):
                self.assertIsNone(self.verified(**changes))

    def test_missing_group_is_unassigned(self):
        with self.assertRaises(PermissionError):
            self.verified(**{"cognito:groups": '[]'})

    def test_group_precedence_admin_above_operator(self):
        self.assertEqual(self.verified(**{"cognito:groups": '["viewer","admin","operator"]'}).role, "admin")

    def test_read_only_token_cannot_mutate_even_with_admin_group(self):
        principal = self.verified(scope="orderflow/read", **{"cognito:groups": '["admin"]'})
        self.assertEqual(authorize("POST", "/api/orders", principal)[0], 403)
        self.assertFalse(principal.session()["permissions"]["can_create"])

    def test_operator_cannot_restock_or_run_drill(self):
        principal = self.verified()
        for path in ("/api/inventory/TOTE-001/adjust", "/api/operations/drill"):
            self.assertEqual(authorize("POST", path, principal)[0], 403)
        self.assertIsNone(authorize("POST", "/api/orders", principal))

    def test_expired_principal_cannot_read_orders(self):
        self.assertEqual(authorize("GET", "/api/orders", Principal("a", "viewer", "Viewer", "cognito", 1))[0], 401)

    def test_config_never_returns_private_environment(self):
        config = cognito_config({**self.env, "AWS_SECRET_ACCESS_KEY": "private-value"})
        self.assertNotIn("private-value", str(config))

    def test_local_rotation_csrf_expiry_and_bounded_session_count(self):
        clock = [100]
        store = SessionStore(lifetime=10, clock=lambda: clock[0], maximum=2)
        first, session = store.create("operator")
        self.assertTrue(store.valid_csrf(first, session["csrf_token"]))
        self.assertFalse(store.valid_csrf(first, "x" * 43))
        second, _ = store.create("viewer", previous_token=first)
        self.assertIsNone(store.get(first))
        store.create("admin")
        store.create("operator")
        self.assertIsNone(store.get(second))
        clock[0] += 11
        self.assertFalse(store._sessions == {})
        self.assertIsNone(store.get(first))
        self.assertEqual(len(store._sessions), 0)
