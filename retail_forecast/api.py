"""Framework-independent request handling, shared by local HTTP and AWS Lambda."""

from __future__ import annotations

import math

from .inventory import compare_policies
from .storage import MIN_CUTOFF

DEFAULT_ASSUMPTIONS = {"initial_stock": 120, "lead_time": 7, "review_period": 7, "safety_days": 3,
                       "unit_cost": 4, "holding_cost": 0.02, "stockout_cost": 2, "order_cost": 5}
BOUNDS = {"initial_stock": (0, 1000000), "lead_time": (0, 28), "review_period": (1, 28),
          "safety_days": (0, 28), "unit_cost": (0, 10000), "holding_cost": (0, 1000),
          "stockout_cost": (0, 10000), "order_cost": (0, 10000)}


def _integer(value, name):
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a whole number")
    try:
        number = float(value)
        if not math.isfinite(number) or number != int(number):
            raise ValueError()
        return int(number)
    except (ValueError, TypeError, OverflowError):
        raise ValueError(f"{name} must be a whole number") from None


def _selection(values, repository):
    catalog = repository.catalog()
    store = values.get("store_id", values.get("store"))
    item = values.get("item_id", values.get("item"))
    if not isinstance(store, str) or not isinstance(item, str) or len(store) > 80 or len(item) > 80:
        raise ValueError("A valid store and product are required")
    if not any(p["store_id"] == store and p["item_id"] == item for p in catalog["products"]):
        raise LookupError("This store/product pair is not in the dataset")
    cutoff = _integer(values.get("cutoff", catalog["default_cutoff"]), "cutoff")
    if not MIN_CUTOFF <= cutoff <= catalog["total_days"]:
        raise ValueError(f"cutoff must be between {MIN_CUTOFF} and {catalog['total_days']}")
    model = values.get("model", "seasonal")
    if not any(m["id"] == model and m["available"] for m in catalog["models"]):
        raise ValueError("Selected model is unavailable; install the ML extra to enable XGBoost locally")
    return store, item, cutoff, model


def _assumptions(body):
    provided = body.get("assumptions", {})
    if not isinstance(provided, dict):
        raise ValueError("assumptions must be a JSON object")
    unknown = set(provided) - set(DEFAULT_ASSUMPTIONS)
    if unknown:
        raise ValueError("Unknown inventory assumption: " + ", ".join(sorted(unknown)))
    assumptions = {**DEFAULT_ASSUMPTIONS, **provided}
    for key, value in assumptions.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{key} must be a finite number")
        low, high = BOUNDS[key]
        if not low <= value <= high:
            raise ValueError(f"{key} must be between {low} and {high}")
        if not math.isfinite(value):
            raise ValueError(f"{key} must be a finite number")
        if key in ("initial_stock", "lead_time", "review_period", "safety_days"):
            assumptions[key] = _integer(value, key)
    return assumptions


def handle_request(method, path, query, body, repository):
    try:
        if method == "GET" and path == "/api/health":
            return 200, {"status": "ok", "mode": repository.mode}
        if method == "GET" and path == "/api/catalog":
            return 200, repository.catalog()
        if method == "GET" and path == "/api/forecast":
            result = repository.forecast(*_selection(query, repository))
            return 200, {k: v for k, v in result.items() if not k.startswith("_")}
        if method == "POST" and path == "/api/simulate":
            if not isinstance(body, dict):
                raise ValueError("Request body must be a JSON object")
            assumptions = _assumptions(body)
            result = repository.forecast(*_selection(body, repository))
            if len(result.get("_actuals", [])) != 28:
                raise ValueError("Simulation requires all 28 held-out historical days; choose an earlier replay date")
            return 200, {**compare_policies(result["_actuals"], result["forecast"], assumptions),
                         "source": result["source"], "source_label": result["source_label"],
                         "as_of": result["as_of"], "model": result["model"]}
        if method == "GET" and path == "/api/releases":
            return 200, repository.releases()
        if method == "POST" and path == "/api/releases/demo":
            return 200, repository.demo_releases()
        return 404, {"error": "API route not found"}
    except ValueError as exc:
        return 400, {"error": str(exc)}
    except LookupError as exc:
        return 404, {"error": str(exc)}
    except PermissionError as exc:
        return 403, {"error": str(exc)}
