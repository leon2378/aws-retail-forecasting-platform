"""Run failure evidence on isolated synthetic inventory and persist only proof."""
import os
import tempfile
from pathlib import Path

from aws.repository import DynamoRepository


def handler(event, context):
    from orderflow.engine import Engine
    from orderflow.storage import SQLiteRepository

    repository = DynamoRepository(os.environ["ORDERFLOW_TABLE"])
    drill_id = event["drill_id"]
    if "failure" in event:
        repository.transact(lambda state: state["drills"][drill_id].update({
            "status": "failed", "passed": False, "error": "The recovery workflow failed before evidence was completed."}))
        return {"drill_id": drill_id, "passed": False, "status": "failed"}
    try:
        parent = Path("/tmp") if os.environ.get("AWS_LAMBDA_FUNCTION_NAME") else Path.cwd() / "build"
        parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="orderflow-drill-", dir=parent) as directory:
            engine = Engine(SQLiteRepository(Path(directory) / "isolated.sqlite3"))
            proof = engine.run_drill(actor=event["actor"], drill_id=drill_id)
        proof["execution"] = "AWS Step Functions Standard"
        def record(state):
            previous = state["drills"].get(drill_id, {})
            state["drills"][drill_id] = {**previous, **proof}
            while len(state["drills"]) > 20:
                oldest = min(state["drills"], key=lambda key: state["drills"][key]["at"])
                del state["drills"][oldest]
        repository.transact(record)
        return {"drill_id": drill_id, "passed": bool(proof.get("passed")), "status": proof.get("status")}
    except Exception:
        repository.transact(lambda state: state["drills"][drill_id].update({
            "status": "failed", "passed": False, "error": "The isolated drill could not finish."}))
        raise
