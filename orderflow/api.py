"""Transport-independent API with server-side role enforcement."""

import re

from . import __version__
from .auth import anonymous_session, authorize, local_config
from .errors import OrderFlowError


def _empty(body):
    if body != {}:
        raise OrderFlowError(400, "invalid_input", "This action accepts an empty JSON object.")


def handle_request(method, path, query, body, engine, principal=None, config=None):
    config = config or local_config()
    denied = authorize(method, path, principal)
    if denied:
        return denied
    if method == "GET" and path == "/api/health":
        return 200, {"status": "ok", "service": "OrderFlow", "version": __version__,
                     "mode": getattr(engine, "mode", "aws" if config.get("mode") == "cognito" else config.get("mode", "local_demo")), "simulation": True}
    if method == "GET" and path == "/api/auth/config":
        return 200, config
    if method == "GET" and path == "/api/auth/session":
        return 200, principal.session() if principal else anonymous_session()
    try:
        if method == "GET":
            if path == "/api/dashboard":
                return 200, engine.dashboard()
            if path == "/api/catalog":
                return 200, engine.catalog()
            if path == "/api/orders":
                return 200, engine.orders(query.get("status"), query.get("search", ""))
            if path == "/api/operations":
                return 200, engine.operations()
            match = re.fullmatch(r"/api/orders/([A-Za-z0-9_-]{1,64})", path)
            if match:
                return 200, {"order": engine.get_order(match[1])}
            match = re.fullmatch(r"/api/operations/drills/([A-Za-z0-9_-]{1,64})", path)
            if match:
                drill = engine.repository.load()["drills"].get(match[1])
                return (200, {"drill": drill}) if drill else (404, {"error": "That drill has not completed or does not exist.", "code": "not_found"})
        if method == "POST":
            if not isinstance(body, dict):
                return 400, {"error": "Request body must be a JSON object.", "code": "invalid_json"}
            actor = principal.subject
            if path == "/api/orders":
                order, replayed = engine.submit(body, actor)
                return (200 if replayed else 201), {"order": order, "replayed": replayed}
            if path == "/api/operations/process":
                if engine.mode != "local_demo":
                    return 409, {"error": "AWS workers process queued work automatically.", "code": "local_only"}
                if set(body) - {"limit"}:
                    raise OrderFlowError(400, "invalid_input", "Processing accepts a limit only.")
                return 200, engine.process(body.get("limit", 20), actor)
            if path == "/api/operations/drill":
                _empty(body)
                return 200, {"drill": engine.run_drill(actor)}
            match = re.fullmatch(r"/api/orders/([A-Za-z0-9_-]{1,64})/cancel", path)
            if match:
                _empty(body)
                return 200, {"order": engine.cancel(match[1], actor)}
            match = re.fullmatch(r"/api/operations/retry/([A-Za-z0-9_-]{1,64})", path)
            if match:
                _empty(body)
                return 200, {"order": engine.retry(match[1], actor)}
            match = re.fullmatch(r"/api/inventory/([A-Za-z0-9_-]{1,64})/adjust", path)
            if match:
                if set(body) != {"quantity"}:
                    raise OrderFlowError(400, "invalid_input", "Restocking accepts a quantity only.")
                return 200, {"product": engine.restock(match[1], body["quantity"], actor)}
        return 404, {"error": "That route does not exist.", "code": "not_found"}
    except OrderFlowError as error:
        return error.status, {"error": str(error), "code": error.code}
