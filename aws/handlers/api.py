"""HTTP API adapter: trust only API Gateway's verified Cognito claims."""
import base64
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from urllib.parse import parse_qs

from aws.repository import DynamoRepository, MAX_ACCEPTED_ORDERS, RepositoryUnavailable

log = logging.getLogger(__name__)
MAX_BODY_BYTES = 16_384


def response(status, payload):
    return {"statusCode": status, "headers": {"content-type": "application/json",
            "cache-control": "no-store", "x-content-type-options": "nosniff"},
            "body": json.dumps(payload, ensure_ascii=False, allow_nan=False)}


def failure(status, code, message):
    return response(status, {"error": message, "code": code})


def start_drill(repository, principal, client, machine_arn):
    drill_id = "drill_" + uuid.uuid4().hex
    started = datetime.now(timezone.utc).isoformat()
    record = {"id": drill_id, "drill_id": drill_id, "status": "running", "passed": False,
              "started_at": started, "actor": principal.subject, "simulation": True,
              "at": started, "scope": "isolated", "checks": [], "steps": [], "duration_ms": 0,
              "execution": "AWS Step Functions Standard"}
    def reserve(state):
        state["drills"][drill_id] = record
        while len(state["drills"]) > 20:
            oldest = min(state["drills"], key=lambda key: state["drills"][key]["at"])
            del state["drills"][oldest]
    repository.transact(reserve)
    try:
        execution = client.start_execution(stateMachineArn=machine_arn, name=drill_id,
            input=json.dumps({"drill_id": drill_id, "actor": principal.subject}))
        repository.transact(lambda state: state["drills"][drill_id].update({"execution_arn": execution["executionArn"]}))
    except Exception:
        repository.transact(lambda state: state["drills"][drill_id].update({
            "status": "failed", "error": "Recovery workflow could not be started."}))
        raise
    return response(202, {"drill_id": drill_id, "status": "running"})


def handler(event, context):
    from orderflow.api import handle_request
    from orderflow.auth import authorize, cognito_config, verified_cognito_principal
    from orderflow.engine import Engine

    try:
        method = event.get("requestContext", {}).get("http", {}).get("method", "GET").upper()
        path = event.get("rawPath", "/")
        public = (method, path) in {("GET", "/api/health"), ("GET", "/api/auth/config")}
        config = cognito_config(os.environ)
        principal = None if public else verified_cognito_principal(event, os.environ)
        if not public and principal is None:
            return failure(401, "unauthorized", "Sign in to access OrderFlow.")
        denied = authorize(method, path, principal)
        if denied:
            return response(*denied)
        if not public and method not in {"GET", "HEAD", "OPTIONS"}:
            scopes = event.get("requestContext", {}).get("authorizer", {}).get("jwt", {}).get("claims", {}).get("scope", "").split()
            if "orderflow/write" not in scopes:
                return failure(403, "scope_required", "Write permission is required.")
        raw = event.get("body") or ""
        if not isinstance(raw, str):
            return failure(400, "invalid_body", "Request body must be JSON text.")
        if event.get("isBase64Encoded") and len(raw) > 4 * ((MAX_BODY_BYTES + 2) // 3):
            return failure(413, "body_too_large", "Request body is too large.")
        if event.get("isBase64Encoded"):
            try:
                raw = base64.b64decode(raw, validate=True).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                return failure(400, "invalid_body", "Request body is not valid UTF-8 JSON.")
        if len(raw.encode("utf-8")) > MAX_BODY_BYTES:
            return failure(413, "body_too_large", "Request body is too large.")
        try:
            body = json.loads(raw, parse_constant=lambda value: (_ for _ in ()).throw(ValueError("Non-finite JSON value"))) if raw else {}
        except (ValueError, TypeError, RecursionError):
            return failure(400, "invalid_json", "Request body must contain valid JSON.")
        if not isinstance(body, dict):
            return failure(400, "invalid_body", "Request body must be an object.")
        if method == "POST" and path == "/api/orders":
            header = next((value for key, value in event.get("headers", {}).items() if key.lower() == "idempotency-key"), None)
            if header is not None:
                if "idempotency_key" in body and body["idempotency_key"] != header:
                    return failure(400, "invalid_input", "Idempotency-Key must match the body key.")
                body["idempotency_key"] = header
        query = event.get("queryStringParameters") or {key: values[-1] for key, values in parse_qs(event.get("rawQueryString", "")).items()}
        engine = None if public else Engine(DynamoRepository(os.environ["ORDERFLOW_TABLE"]))
        if method == "POST" and path == "/api/operations/drill":
            if principal.role != "admin":
                return failure(403, "forbidden", "Administrator permission is required.")
            if body:
                return failure(400, "invalid_body", "The recovery drill takes an empty object.")
            import boto3
            return start_drill(engine.repository, principal, boto3.client("stepfunctions"), os.environ["DRILL_MACHINE_ARN"])
        if method == "GET" and path == "/api/health":
            config = {**config, "mode": "aws"}
        status, payload = handle_request(method, path, query, body, engine, principal, config)
        if method == "GET" and path == "/api/operations" and status == 200:
            payload["limits"] = {**payload.get("limits", {}), "orders": MAX_ACCEPTED_ORDERS}
        return response(status, payload)
    except RepositoryUnavailable:
        log.exception("workspace_unavailable")
        return failure(503, "workspace_unavailable", "Workspace is unavailable or at capacity. Contact the operator.")
    except PermissionError as exc:
        return failure(403, "access_unassigned", str(exc))
    except Exception as exc:
        if hasattr(exc, "status") and hasattr(exc, "code"):
            return failure(exc.status, exc.code, str(exc))
        log.exception("request_failed request_id=%s", event.get("requestContext", {}).get("requestId", "unknown"))
        return failure(500, "internal_error", "The request could not be completed.")
