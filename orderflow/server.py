"""Loopback-only local UI with role-preview sessions and CSRF protection."""

from functools import partial
from http.cookies import CookieError, SimpleCookie
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import math
from pathlib import Path
import socket
from urllib.parse import parse_qs, urlsplit

from .api import handle_request
from .auth import COOKIE_NAME, SessionStore, anonymous_session, local_config, loopback_host, request_origin
from .errors import OrderFlowError

STATIC_PATHS = {"/", "/index.html", "/styles.css", "/app.js", "/auth.js", "/config.js", "/helpers.js"}
logger = logging.getLogger(__name__)


class Handler(SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        super().end_headers()

    def _json(self, status, payload, cookie=None):
        raw = json.dumps(payload, allow_nan=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        if cookie is not None:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(raw)

    def _origin_allowed(self, mutation=False):
        hosts, origins = self.headers.get_all("Host", []), self.headers.get_all("Origin", [])
        return len(hosts) == 1 and len(origins) <= 1 and request_origin(hosts[0], origins[0] if origins else None,
            self.headers.get("Sec-Fetch-Site") if mutation else None, self.server.server_port, mutation)

    def _session_token(self):
        try:
            cookie = SimpleCookie()
            cookie.load(self.headers.get("Cookie", ""))
            return cookie[COOKIE_NAME].value if COOKIE_NAME in cookie else None
        except CookieError:
            return None

    @staticmethod
    def _cookie(token=None, lifetime=0):
        return f"{COOKIE_NAME}={token or ''}; HttpOnly; SameSite=Strict; Path=/; Max-Age={lifetime}"

    def _api(self):
        if not self._origin_allowed(self.command == "POST"):
            self._json(403, {"error": "Use the same local origin for OrderFlow requests.", "code": "invalid_origin"})
            return
        parsed = urlsplit(self.path)
        query = {key: values[-1] for key, values in parse_qs(parsed.query).items()}
        body = {}
        if self.command == "POST":
            if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
                self._json(415, {"error": "Use Content-Type: application/json.", "code": "invalid_content_type"})
                return
            try:
                lengths = self.headers.get_all("Content-Length", [])
                if len(lengths) != 1 or self.headers.get("Transfer-Encoding"):
                    raise ValueError("A single Content-Length is required.")
                length = int(lengths[0])
                if not 0 <= length <= 16384:
                    self._json(413, {"error": "Request body exceeds 16 KB.", "code": "body_too_large"})
                    return
                body = json.loads(self.rfile.read(length) or b"{}", parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Non-finite JSON")))
                if not isinstance(body, dict):
                    raise ValueError("JSON object required.")
            except (ValueError, UnicodeDecodeError, RecursionError):
                self._json(400, {"error": "Request body must be a valid JSON object.", "code": "invalid_json"})
                return
        try:
            token = self._session_token()
            sessions = self.server.sessions
            session = sessions.get(token)
            principal = session["principal"] if session else None
            if self.command == "GET" and parsed.path == "/api/auth/session":
                payload = principal.session(session["csrf"]) if session else anonymous_session()
                if session:
                    payload["expires_in"] = max(0, math.ceil(session["expires"] - sessions.clock()))
                self._json(200, payload)
                return
            if self.command == "POST" and parsed.path == "/api/auth/login":
                if set(body) != {"role"}:
                    raise ValueError("Local role preview accepts only a role.")
                new_token, payload = sessions.create(body["role"], previous_token=token)
                self._json(200, payload, self._cookie(new_token, sessions.lifetime))
                return
            if self.command == "POST":
                if principal is None:
                    self._json(401, {"error": "Sign in to access OrderFlow.", "code": "unauthorized"})
                    return
                if not sessions.valid_csrf(token, self.headers.get("X-CSRF-Token")):
                    self._json(403, {"error": "Session verification failed. Refresh and try again.", "code": "invalid_csrf"})
                    return
                if parsed.path == "/api/auth/logout":
                    if body:
                        raise ValueError("Sign-out accepts an empty JSON object.")
                    sessions.delete(token)
                    self._json(200, anonymous_session(), self._cookie())
                    return
                if parsed.path == "/api/orders" and self.headers.get("Idempotency-Key"):
                    key = self.headers["Idempotency-Key"]
                    if "idempotency_key" in body and body["idempotency_key"] != key:
                        raise ValueError("Request header and body idempotency keys must agree.")
                    body["idempotency_key"] = key
            status, payload = handle_request(self.command, parsed.path, query, body, self.server.engine, principal, local_config())
            self._json(status, payload)
        except (ValueError, OrderFlowError) as error:
            self._json(getattr(error, "status", 400), {"error": str(error), "code": getattr(error, "code", "invalid_input")})
        except Exception:
            logger.exception("OrderFlow request failed")
            self._json(503, {"error": "OrderFlow is temporarily unavailable.", "code": "unavailable"})

    def _static(self):
        if not self._origin_allowed():
            self._json(403, {"error": "Use the local OrderFlow address.", "code": "invalid_origin"})
        elif urlsplit(self.path).path not in STATIC_PATHS:
            self._json(404, {"error": "That file is not available.", "code": "not_found"})
        elif self.command == "HEAD":
            super().do_HEAD()
        else:
            super().do_GET()

    def do_GET(self):
        self._api() if urlsplit(self.path).path.startswith("/api/") else self._static()

    def do_HEAD(self):
        if urlsplit(self.path).path.startswith("/api/"):
            self._json(405, {"error": "Use GET for API reads.", "code": "method_not_allowed"})
        else:
            self._static()

    def do_POST(self):
        self._api()


def create_server(host, port, engine, directory=None):
    if not loopback_host(host):
        raise ValueError("Local role preview requires 127.0.0.1 or localhost.")
    root = Path(directory or Path(__file__).resolve().parents[1] / "frontend").resolve()
    server_class = ThreadingHTTPServer
    if ":" in host:
        class IPv6Server(ThreadingHTTPServer):
            address_family = socket.AF_INET6
        server_class = IPv6Server
    server = server_class((host, port), partial(Handler, directory=str(root)))
    server.engine, server.sessions = engine, SessionStore()
    server.daemon_threads = True
    return server


def serve(engine, host="127.0.0.1", port=8000):
    server = create_server(host, port, engine)
    print(f"OrderFlow is running at http://{host}:{server.server_port}", flush=True)
    print("Local role preview. Synthetic catalog and simulated payment/shipping only.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
