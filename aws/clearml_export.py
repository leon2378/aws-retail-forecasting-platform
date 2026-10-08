"""Optional experiment comparison import through an SSM-forwarded ClearML API.

Configure CLEARML_API_HOST, CLEARML_WEB_HOST, CLEARML_FILES_HOST and API keys in
your environment. No keys belong in source, Terraform state or an image.
"""
import argparse
import json
import tempfile
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evaluation-s3", required=True, help="s3://bucket/runs/id/evaluate/evaluation/evaluation.json")
    parser.add_argument("--artifacts-s3", required=True, help="s3://bucket/clearml/")
    parser.add_argument("--name", required=True)
    args = parser.parse_args()
    if not args.evaluation_s3.startswith("s3://") or not args.artifacts_s3.startswith("s3://"):
        parser.error("Evaluation and artifacts must use S3 URLs")
    import boto3
    from clearml import Task
    bucket, key = args.evaluation_s3[5:].split("/", 1)
    body = boto3.client("s3").get_object(Bucket=bucket, Key=key)["Body"].read()
    report = json.loads(body)
    task = Task.init(project_name="Retail forecasting", task_name=args.name, task_type=Task.TaskTypes.testing,
                     output_uri=args.artifacts_s3, auto_connect_frameworks=False, auto_connect_arg_parser=False)
    task.connect({"source_evaluation": args.evaluation_s3, "orchestrator": "SageMaker Pipelines", "source": report["source"]})
    for metric in ["wape", "baseline_wape", "coverage"]:
        if report.get(metric) is not None:
            task.get_logger().report_single_value(metric, report[metric])
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "evaluation.json"
        path.write_bytes(body)
        task.upload_artifact("release-evaluation", artifact_object=str(path), wait_on_upload=True)
    task.close()


if __name__ == "__main__":
    main()
