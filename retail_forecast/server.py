"""Dependency-free local server. Production uses API Gateway and Lambda."""

from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .api import handle_request
from .storage import LocalRepository


class Handler(SimpleHTTPRequestHandler):
    repository = None

    def _json(self, status, value):
        payload = json.dumps(value, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(payload)

    def _api(self):
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
            status, result = handle_request(self.command, parsed.path, query, body, self.repository)
            self._json(status, result)
        except Exception:
            import traceback
            traceback.print_exc()
            self._json(500, {"error": "Request failed; see the server log for details"})

    def do_GET(self):
        if urlsplit(self.path).path.startswith("/api/"):
            self._api()
        else:
            super().do_GET()

    def do_POST(self):
        self._api()


def serve(host="127.0.0.1", port=8000, data_dir="data"):
    root = Path(__file__).resolve().parent.parent / "frontend"
    Handler.repository = LocalRepository(data_dir)
    server = ThreadingHTTPServer((host, port), partial(Handler, directory=str(root)))
    server.daemon_threads = True
    print(f"Shelfcast is running at http://{host}:{port}", flush=True)
    print(f"Data: {Handler.repository.dataset['source_label']}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
