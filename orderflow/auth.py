"""Explicit role permissions and bounded, loopback-only demonstration sessions.

AWS accepts claims supplied by API Gateway's JWT authorizer. This module never
decodes a bearer token or treats client-supplied headers as verified identity.
"""

from dataclasses import dataclass
from hashlib import sha256
import ipaddress
import json
import math
import re
import secrets
import threading
import time
from urllib.parse import urlsplit


ROLES = ("viewer", "operator", "admin")
PERMISSIONS = ("can_create", "can_retry", "can_drill", "can_manage_inventory")
COOKIE_NAME = "orderflow_session"
TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")
PUBLIC_ROUTES = {("GET", "/api/health"), ("GET", "/api/auth/config"), ("GET", "/api/auth/session")}



@dataclass(frozen=True)
class Principal:
    subject: str
    role: str
    display_name: str
    source: str
    expires_at: int | None = None
    scopes: tuple[str, ...] = ()

    def __post_init__(self):
        if self.role not in ROLES or self.source not in ("local_demo", "cognito"):
            raise ValueError("Identity must have a recognized role and source")
        if not isinstance(self.subject, str) or not self.subject or len(self.subject) > 256:
            raise ValueError("Identity must have a bounded subject")
        if self.expires_at is not None and (isinstance(self.expires_at, bool)
                or not isinstance(self.expires_at, int) or self.expires_at <= 0):
            raise ValueError("Identity expiry must be a positive epoch timestamp")

    def session(self, csrf_token=None):
        session = {"authenticated": True, "role": self.role, "display_name": self.display_name,
                   "permissions": {name: ((self.source == "local_demo" or "orderflow/write" in self.scopes)
                       and (self.role == "admin" or (self.role == "operator" and name in ("can_create", "can_retry")))) for name in PERMISSIONS}}
        if csrf_token is not None:
            session["csrf_token"] = csrf_token
        if self.expires_at is not None:
            session["expires_in"] = max(0, math.ceil(self.expires_at - time.time()))
        return session


def anonymous_session():
    return {"authenticated": False, "role": None, "display_name": None,
            "permissions": {name: False for name in PERMISSIONS}}


def local_config():
    return {"mode": "local_demo", "enabled": True, "session_endpoint": "/api/auth/session",
            "login_endpoint": "/api/auth/login", "logout_endpoint": "/api/auth/logout",
            "roles": list(ROLES),
            "notice": "Local demonstration only. No credentials or production identity are verified."}


def cognito_config(environment):
    """Return public settings only; never expose credentials or an account ID."""
    fields = {"issuer": environment.get("AUTH_ISSUER", ""),
              "client_id": environment.get("AUTH_CLIENT_ID", ""),
              "domain": environment.get("AUTH_DOMAIN", ""),
              "callback_url": environment.get("AUTH_CALLBACK_URL", ""),
              "logout_url": environment.get("AUTH_LOGOUT_URL", "")}
    configured = all(isinstance(value, str) and value for value in fields.values())
    enabled = environment.get("AUTH_ENABLED", "true").lower() == "true" and configured
    return {"mode": "cognito", "enabled": enabled, "session_endpoint": "/api/auth/session",
            **fields, "scopes": environment.get("AUTH_SCOPES", "openid profile").split()}


def authorize(method, path, principal):
    if (method, path) in PUBLIC_ROUTES:
        return None
    if not isinstance(principal, Principal):
        return 401, {"error": "Sign in to access OrderFlow.", "code": "unauthorized"}
    if principal.expires_at is not None and principal.expires_at <= time.time():
        return 401, {"error": "Your session expired. Sign in again.", "code": "session_expired"}
    if method == "POST" and principal.source == "cognito" and "orderflow/write" not in principal.scopes:
        return 403, {"error": "The access token does not include write permission.", "code": "scope_required"}
    if method == "POST" and principal.role == "viewer":
        return 403, {"error": "Operator permission is required for this action.", "code": "forbidden"}
    if method == "POST" and (path == "/api/operations/drill" or path.startswith("/api/inventory/")) and principal.role != "admin":
        return 403, {"error": "Administrator permission is required for this action.", "code": "forbidden"}
    return None


def _timestamp(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) and result > 0 and result.is_integer() else None
    except (ValueError, OverflowError):
        return None


def _groups(value):
    if isinstance(value, str):
        if len(value) > 4096:
            return []
        try:
            parsed = json.loads(value)
            value = parsed if isinstance(parsed, list) else value.split(",")
        except ValueError:
            # Gateway can stringify an array without JSON quotes around names.
            value = value[1:-1].replace(",", " ").split() if value.startswith("[") and value.endswith("]") else value.split(",")
    if not isinstance(value, list) or len(value) > 100 or not all(isinstance(v, str) for v in value):
        return []
    return [v.strip() for v in value]


def verified_cognito_principal(event, environment, now=None):
    """Trust only the Gateway authorizer boundary, then enforce access-token claims.

    The Lambda must only be invokable by its configured API Gateway integration.
    Terraform restricts that invocation; this is not a standalone JWT verifier.
    """
    if not cognito_config(environment)["enabled"]:
        return None
    context = event.get("requestContext")
    if not isinstance(context, dict):
        return None
    authorizer = context.get("authorizer", {})
    jwt = authorizer.get("jwt", {}) if isinstance(authorizer, dict) else {}
    claims = jwt.get("claims", {}) if isinstance(jwt, dict) else {}
    if not isinstance(claims, dict):
        return None
    if claims.get("token_use") != "access" or claims.get("iss") != environment.get("AUTH_ISSUER"):
        return None
    if claims.get("client_id") != environment.get("AUTH_CLIENT_ID"):
        return None
    scope = claims.get("scope")
    if not isinstance(scope, str) or len(scope) > 4096 or "orderflow/read" not in scope.split():
        return None
    now = time.time() if now is None else now
    expiry, issued = _timestamp(claims.get("exp")), _timestamp(claims.get("iat"))
    if expiry is None or issued is None or expiry <= now or issued > now + 60 or expiry <= issued:
        return None
    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject or len(subject) > 256 or any(ord(c) < 32 for c in subject):
        return None
    groups = _groups(claims.get("cognito:groups"))
    role = "admin" if "admin" in groups else "operator" if "operator" in groups else "viewer" if "viewer" in groups else None
    if role is None:
        raise PermissionError("Access is not assigned. Ask an administrator to add you to viewer, operator or admin.")
    return Principal(subject, role, f"{role.title()} account", "cognito", int(expiry), tuple(scope.split()))


def loopback_host(host):
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def request_origin(host_header, origin, fetch_site, port, require_origin=False):
    """Reject DNS rebinding and cross-origin browser mutations on the local server."""
    try:
        host = urlsplit("http://" + (host_header or ""))
        if host.username or host.password or host.path or host.query or host.fragment:
            return False
        if not host.hostname or not loopback_host(host.hostname) or (host.port or 80) != port:
            return False
        if fetch_site is not None and fetch_site != "same-origin":
            return False
        if origin is None:
            return not require_origin
        parsed = urlsplit(origin)
        return (parsed.scheme == "http" and parsed.netloc == host.netloc
                and not parsed.path and not parsed.query and not parsed.fragment)
    except (TypeError, ValueError):
        return False


class SessionStore:
    """In-memory sessions expire after an hour and disappear when the server stops."""

    def __init__(self, lifetime=3600, clock=time.monotonic, maximum=128):
        self.lifetime, self.clock, self.maximum = lifetime, clock, maximum
        self._sessions = {}
        self._lock = threading.RLock()

    @staticmethod
    def _key(token):
        if not isinstance(token, str) or TOKEN_PATTERN.fullmatch(token) is None:
            return None
        return sha256(token.encode("ascii")).digest()

    def _prune(self):
        now = self.clock()
        self._sessions = {key: value for key, value in self._sessions.items() if value["expires"] > now}

    def create(self, role, previous_token=None):
        if role not in ROLES:
            raise ValueError("Choose viewer, operator or admin for the local demonstration")
        with self._lock:
            self._prune()
            self.delete(previous_token)
            while len(self._sessions) >= self.maximum:
                self._sessions.pop(next(iter(self._sessions)))
            token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            principal = Principal("local-" + role, role, f"Local demo {role}", "local_demo")
            self._sessions[self._key(token)] = {"principal": principal, "csrf": csrf,
                                                "expires": self.clock() + self.lifetime}
            return token, {**principal.session(csrf), "expires_in": self.lifetime}

    def get(self, token):
        with self._lock:
            self._prune()
            return self._sessions.get(self._key(token))

    def delete(self, token):
        with self._lock:
            self._sessions.pop(self._key(token), None)

    def valid_csrf(self, token, csrf):
        session = self.get(token)
        return bool(session and isinstance(csrf, str) and TOKEN_PATTERN.fullmatch(csrf)
                    and secrets.compare_digest(session["csrf"], csrf))
