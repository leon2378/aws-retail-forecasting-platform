"""Dependency-free local server. Production uses API Gateway and Lambda."""

from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import CookieError, SimpleCookie
import json
import math
from pathlib import Path
import socket
from urllib.parse import parse_qs, urlsplit

from .api import handle_request
from .auth import (COOKIE_NAME, SessionStore, anonymous_session, local_config,
                   loopback_host, request_origin)
from .storage import LocalRepository


class Handler(SimpleHTTPRequestHandler):
    def _json(self, status, value, cookie=None):
        payload = json.dumps(value, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if cookie is not None:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(payload)

    def _origin_allowed(self, require_origin=False):
        hosts = self.headers.get_all("Host", [])
        origins = self.headers.get_all("Origin", [])
        return (len(hosts) == 1 and len(origins) <= 1 and request_origin(
            hosts[0], origins[0] if origins else None,
            self.headers.get("Sec-Fetch-Site") if require_origin else None,
            self.server.server_port, require_origin))

    def _session_token(self):
        try:
            cookie = SimpleCookie()
            cookie.load(self.headers.get("Cookie", ""))
            return cookie[COOKIE_NAME].value if COOKIE_NAME in cookie else None
        except CookieError:
            return None

    @staticmethod
    def _cookie(token=None, lifetime=0):
        value = token or ""
        return f"{COOKIE_NAME}={value}; HttpOnly; SameSite=Strict; Path=/; Max-Age={lifetime}"

    def _api(self):
        if not self._origin_allowed(require_origin=self.command == "POST"):
            self._json(403, {"error": "Use the same local origin for SupplySight requests."})
            return
        parsed = urlsplit(self.path)
        query = {k: v[-1] for k, v in parse_qs(parsed.query).items()}
        body = None
        if self.command == "POST":
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                self._json(415, {"error": "Use Content-Type: application/json"})
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
                if not 0 <= length <= 16384:
                    self._json(413, {"error": "Request body exceeds 16 KB"})
                    return
                body = json.loads(self.rfile.read(length) or b"{}", parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Non-finite JSON")))
            except (ValueError, UnicodeDecodeError):
                self._json(400, {"error": "Request body must contain valid JSON"})
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
                if not isinstance(body, dict) or set(body) != {"role"}:
                    raise ValueError("Local demonstration sign-in accepts only a role")
                created, payload = sessions.create(body["role"], previous_token=token)
                self._json(200, payload, self._cookie(created, sessions.lifetime))
                return
            if self.command == "POST":
                if principal is None:
                    self._json(401, {"error": "Sign in to access SupplySight."})
                    return
                if not sessions.valid_csrf(token, self.headers.get("X-CSRF-Token")):
                    self._json(403, {"error": "Session verification failed. Refresh and try again."})
                    return
                if parsed.path == "/api/auth/logout":
                    if not isinstance(body, dict) or body:
                        raise ValueError("Sign-out accepts an empty JSON object")
                    sessions.delete(token)
                    self._json(200, anonymous_session(), self._cookie())
                    return
            status, result = handle_request(self.command, parsed.path, query, body,
                                            self.server.repository, principal, local_config())
            self._json(status, result)
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
        except Exception:
            import traceback
            traceback.print_exc()
            self._json(500, {"error": "Request failed; see the server log for details"})

    def do_GET(self):
        if urlsplit(self.path).path.startswith("/api/"):
            self._api()
        else:
            if self._origin_allowed():
                super().do_GET()
            else:
                self._json(403, {"error": "Use the local SupplySight address."})

    def do_HEAD(self):
        if self._origin_allowed():
            super().do_HEAD()
        else:
            self._json(403, {"error": "Use the local SupplySight address."})

    def do_POST(self):
        self._api()


def create_server(host, port, repository, directory=None):
    if not loopback_host(host):
        raise ValueError("Local demo sign-in requires a loopback address (127.0.0.1 or localhost)")
    root = Path(__file__).resolve().parent.parent / "frontend"
    server_class = ThreadingHTTPServer
    if ":" in host:
        class IPv6Server(ThreadingHTTPServer):
            address_family = socket.AF_INET6
        server_class = IPv6Server
    server = server_class((host, port), partial(Handler, directory=str(directory or root)))
    server.repository = repository
    server.sessions = SessionStore()
    server.daemon_threads = True
    return server


def serve(host="127.0.0.1", port=8000, data_dir="data"):
    if not loopback_host(host):
        raise ValueError("Local demo sign-in requires a loopback address (127.0.0.1 or localhost)")
    repository = LocalRepository(data_dir)
    server = create_server(host, port, repository)
    address = f"[{host}]" if ":" in host else host
    print(f"SupplySight is running at http://{address}:{server.server_port}", flush=True)
    print(f"Data: {repository.dataset['source_label']}", flush=True)
    print("Sign-in: local role demonstration only; sessions expire after one hour.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
