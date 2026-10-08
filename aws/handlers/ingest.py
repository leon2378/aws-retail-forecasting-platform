"""Idempotent historical-day replay. Only the publisher advances the cursor.

The Dynamo row remembers the request token before StartPipelineExecution. Retrying
after a lost response therefore attaches to the same SageMaker execution.
"""
import json
import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from retail_forecast.quality import MAX_SNAPSHOT_BYTES, oversized_snapshot, parse_snapshot


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
    def bounded_object(object_key):
        response = s3.get_object(Bucket=bucket, Key=object_key)
        body = response["Body"]
        try:
            length = response.get("ContentLength")
            if isinstance(length, int) and length > MAX_SNAPSHOT_BYTES:
                return None
            raw = body.read(MAX_SNAPSHOT_BYTES + 1)
            return raw if len(raw) <= MAX_SNAPSHOT_BYTES else None
        finally:
            body.close()
    source_key = os.environ.get("SOURCE_KEY", "raw/dataset.json")
    snapshot_key = f"runs/{run_id}/source/dataset.json"
    oversized = False
    # Stable snapshot on retries: never copy a changed raw file over this run.
    try:
        metadata = s3.head_object(Bucket=bucket, Key=snapshot_key)
        oversized = metadata.get("ContentLength", 0) > MAX_SNAPSHOT_BYTES
    except ClientError as exc:
        if exc.response["Error"]["Code"] not in {"404", "NoSuchKey", "NotFound"}:
            raise
        try:
            dataset_bytes = bounded_object(source_key)
        except ClientError as source_error:
            if source_error.response["Error"]["Code"] not in {"404", "NoSuchKey", "NotFound"}:
                raise
            dataset_bytes = b""  # Missing source is a rejected, unreadable snapshot.
        oversized = dataset_bytes is None
        if not oversized:
            try:
                s3.put_object(Bucket=bucket, Key=snapshot_key, Body=dataset_bytes, ContentType="application/json",
                              IfNoneMatch="*")
            except ClientError as conflict:
                if conflict.response["Error"]["Code"] not in {"PreconditionFailed", "ConditionalRequestConflict"}:
                    raise
    # Inspect the frozen winner, including after a lost StartPipeline response.
    # The raw object may change between retries without changing this run.
    frozen = None if oversized else bounded_object(snapshot_key)
    oversized = oversized or frozen is None
    previous = table.get_item(Key={"pk": "CATALOG", "sk": "META"}, ConsistentRead=True).get("Item", {})
    expected = previous.get("payload")
    dataset, quality = (None, oversized_snapshot(cutoff=cutoff)) if oversized else parse_snapshot(frozen, expected=expected)
    if quality["passed"] and cutoff + 28 > dataset["total_days"]:
        try:
            table.update_item(Key=key, UpdateExpression="REMOVE run_id, pending_cutoff, started_at",
                              ConditionExpression="run_id = :run", ExpressionAttributeValues={":run": run_id})
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            return {"status": "concurrent_update"}
        return {"status": "replay_complete", "last_cutoff": cutoff - 1}
    if not oversized:
        dataset, quality = parse_snapshot(frozen, cutoff=cutoff, expected=expected)
    quality_key = f"runs/{run_id}/quality/quality.json"
    try:
        s3.put_object(Bucket=bucket, Key=quality_key,
                      Body=json.dumps(quality, allow_nan=False, separators=(",", ":")).encode("utf-8"),
                      ContentType="application/json", IfNoneMatch="*")
    except ClientError as exc:
        if exc.response["Error"]["Code"] not in {"PreconditionFailed", "ConditionalRequestConflict"}:
            raise
        # Preserve the first report and timestamp as immutable run evidence.
        quality = json.loads(s3.get_object(Bucket=bucket, Key=quality_key)["Body"].read())
    status = "passed" if quality["passed"] else "quarantined"
    update = "SET latest_quality = :quality, latest_quality_status = :status, quality_report_uri = :uri"
    if not quality["passed"]:
        update += " REMOVE run_id, pending_cutoff, started_at"
    try:
        table.update_item(Key=key, UpdateExpression=update, ConditionExpression="run_id = :run",
                          ExpressionAttributeValues={":run": run_id, ":status": status,
                              ":uri": f"s3://{bucket}/{quality_key}",
                              ":quality": json.loads(json.dumps(quality, allow_nan=False), parse_float=Decimal)})
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        return {"status": "concurrent_update"}
    if not quality["passed"]:
        return {"status": "quarantined", "cutoff": cutoff, "quality_report_uri": f"s3://{bucket}/{quality_key}"}
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
