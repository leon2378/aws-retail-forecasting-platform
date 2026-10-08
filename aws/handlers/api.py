"""HTTP API adapter. Forecasts are materialized; Lambda never trains models."""
import base64
import json
import logging
import os

from retail_forecast.api import handle_request
from retail_forecast.storage import DynamoRepository

logger = logging.getLogger(__name__)


def handler(event, context):
    method = event.get("requestContext", {}).get("http", {}).get("method", "GET")
    path = event.get("rawPath", "/")
    try:
        raw = event.get("body") or ""
        if event.get("isBase64Encoded"):
            raw = base64.b64decode(raw, validate=True).decode("utf-8")
        if len(raw) > 16_384:
            return response(413, {"error": "Request body is too large."})
        body = json.loads(raw) if raw else {}
        if not isinstance(body, dict):
            return response(400, {"error": "JSON body must be an object."})
    except (ValueError, UnicodeError):
        return response(400, {"error": "Request body must be valid JSON."})
    try:
        repository = DynamoRepository(os.environ["RESULTS_TABLE"])
        status, payload = handle_request(method, path, event.get("queryStringParameters") or {}, body, repository)
        return response(status, payload)
    except Exception:
        logger.exception("API request failed")
        return response(503, {"error": "Forecast service is temporarily unavailable."})


def response(status, payload):
    return {"statusCode": status, "headers": {"content-type": "application/json", "cache-control": "no-store"},
            "body": json.dumps(payload, allow_nan=False, separators=(",", ":"))}
