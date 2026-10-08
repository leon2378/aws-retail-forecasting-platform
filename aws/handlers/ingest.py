"""Idempotent historical-day replay. Only the publisher advances the cursor.

The Dynamo row remembers the request token before StartPipelineExecution. Retrying
after a lost response therefore attaches to the same SageMaker execution.
"""
import json
import os
import uuid
from datetime import datetime, timezone


def handler(event, context):
    import boto3
    from botocore.exceptions import ClientError

    table = boto3.resource("dynamodb").Table(os.environ["RESULTS_TABLE"])
    sm = boto3.client("sagemaker")
    key = {"pk": "REPLAY", "sk": "STATE"}
    state = table.get_item(Key=key, ConsistentRead=True).get("Item", {})
    if state.get("execution_arn"):
        execution = sm.describe_pipeline_execution(PipelineExecutionArn=state["execution_arn"])
        status = execution["PipelineExecutionStatus"]
        if status in {"Executing", "Stopping"}:
            return {"status": "in_progress", "cutoff": int(state["pending_cutoff"])}
        # A failed job or rejected release must retry the SAME historical day.
        # CAS means a delayed invocation cannot clear a newer run's lock.
        try:
            table.update_item(Key=key, UpdateExpression="REMOVE run_id, execution_arn, pending_cutoff, started_at",
                              ConditionExpression="run_id = :run", ExpressionAttributeValues={":run": state["run_id"]})
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            return {"status": "concurrent_update"}
        state = table.get_item(Key=key, ConsistentRead=True).get("Item", {})
    if "run_id" not in state:
        cutoff = int(state.get("cutoff", int(os.environ.get("INITIAL_CUTOFF", "196")) - 1)) + 1
        try:
            table.update_item(Key=key, UpdateExpression="SET run_id = :run, pending_cutoff = :cutoff, started_at = :started",
                              ConditionExpression="attribute_not_exists(run_id)",
                              ExpressionAttributeValues={":run": uuid.uuid4().hex, ":cutoff": cutoff,
                                                         ":started": datetime.now(timezone.utc).isoformat()})
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
        state = table.get_item(Key=key, ConsistentRead=True).get("Item", {})
    run_id, cutoff = state["run_id"], int(state["pending_cutoff"])
    bucket = os.environ["DATA_BUCKET"]
    s3 = boto3.client("s3")
    source_key = os.environ.get("SOURCE_KEY", "raw/dataset.json")
    snapshot_key = f"runs/{run_id}/source/dataset.json"
    # Stable snapshot on retries: never copy a changed raw file over this run.
    try:
        s3.head_object(Bucket=bucket, Key=snapshot_key)
    except ClientError as exc:
        if exc.response["Error"]["Code"] not in {"404", "NoSuchKey", "NotFound"}:
            raise
        raw = s3.get_object(Bucket=bucket, Key=source_key)
        dataset_bytes = raw["Body"].read()
        dataset = json.loads(dataset_bytes)
        if cutoff + 28 > dataset["total_days"]:
            table.update_item(Key=key, UpdateExpression="REMOVE run_id, pending_cutoff, started_at",
                              ConditionExpression="run_id = :run", ExpressionAttributeValues={":run": run_id})
            return {"status": "replay_complete", "last_cutoff": cutoff - 1}
        try:
            s3.put_object(Bucket=bucket, Key=snapshot_key, Body=dataset_bytes, ContentType="application/json",
                          IfNoneMatch="*")
        except ClientError as conflict:
            if conflict.response["Error"]["Code"] not in {"PreconditionFailed", "ConditionalRequestConflict"}:
                raise
    result = sm.start_pipeline_execution(
        PipelineName=os.environ["PIPELINE_NAME"], ClientRequestToken=run_id,
        PipelineExecutionDisplayName=f"day-{cutoff}-{run_id[:8]}",
        PipelineParameters=[{"Name": "RunId", "Value": run_id}, {"Name": "Cutoff", "Value": str(cutoff)},
                            {"Name": "SnapshotUri", "Value": f"s3://{bucket}/{snapshot_key}"},
                            {"Name": "CandidateModel", "Value": os.environ.get("CANDIDATE_MODEL", "seasonal")}])
    try:
        table.update_item(Key=key, UpdateExpression="SET execution_arn = :arn", ConditionExpression="run_id = :run",
                          ExpressionAttributeValues={":arn": result["PipelineExecutionArn"], ":run": run_id})
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
    return {"status": "started", "cutoff": cutoff, "execution_arn": result["PipelineExecutionArn"]}
