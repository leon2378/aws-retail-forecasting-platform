"""Generate a SageMaker DAG without a dependency on SageMaker's Python SDK.

The result is ordinary JSON accepted by boto3 create_pipeline/update_pipeline.
All job artifacts are isolated below runs/<RunId>; no shared mutable model key.
"""
import argparse
import json
import re
from pathlib import Path


def get(path):
    return {"Get": path}


def join(*values):
    return {"Std:Join": {"On": "", "Values": list(values)}}


def build_pipeline(*, name, role_arn, image_uri, bucket, table_name, model_group, instance_type="ml.m5.xlarge"):
    # The longest suffix is '-forecast-' followed by a 32-character UUID.
    # SageMaker job and model names have a 63-character service limit.
    if not re.fullmatch(r"[a-z][a-z0-9-]{1,19}[a-z0-9]", name):
        raise ValueError("name must be 3-21 lowercase letters, numbers or hyphens, starting with a letter and ending with a letter or number")
    if not image_uri or not role_arn.startswith("arn:") or not bucket:
        raise ValueError("A container image URI, AWS role ARN and S3 bucket are required")
    run = get("Parameters.RunId")
    root = join(f"s3://{bucket}/runs/", run)
    cutoff = get("Parameters.Cutoff")
    training_data = get("Steps.Prepare.ProcessingOutputConfig.Outputs['history'].S3Output.S3Uri")
    model_data = get("Steps.Train.ModelArtifacts.S3ModelArtifacts")

    def job_name(stage):
        return join(f"{name}-{stage}-", run)

    def processing(stage, args, inputs=(), outputs=(), depends=()):
        arguments = {
            "ProcessingJobName": job_name(stage.lower()), "RoleArn": role_arn,
            "AppSpecification": {"ImageUri": image_uri, "ContainerEntrypoint": ["python", "-m", "aws.jobs"],
                                 "ContainerArguments": [stage.lower(), *args]},
            "ProcessingResources": {"ClusterConfig": {"InstanceCount": 1, "InstanceType": instance_type, "VolumeSizeInGB": 30}},
            "StoppingCondition": {"MaxRuntimeInSeconds": 3600},
            "Environment": {"RESULTS_TABLE": table_name},
        }
        if inputs:
            arguments["ProcessingInputs"] = [{"InputName": label, "S3Input": {"S3Uri": uri,
                "LocalPath": f"/opt/ml/processing/input/{label}", "S3DataType": "S3Prefix", "S3InputMode": "File",
                "S3DataDistributionType": "FullyReplicated"}} for label, uri in inputs]
        if outputs:
            arguments["ProcessingOutputConfig"] = {"Outputs": [{"OutputName": label, "S3Output": {
                "S3Uri": join(root, f"/{stage.lower()}/{label}"), "LocalPath": f"/opt/ml/processing/output/{label}",
                "S3UploadMode": "EndOfJob"}} for label in outputs]}
        result = {"Name": stage, "Type": "Processing", "Arguments": arguments}
        if depends:
            result["DependsOn"] = list(depends)
        return result

    prepare = processing("Prepare", ["--cutoff", join(cutoff), "--model", get("Parameters.CandidateModel")],
                         inputs=[("source", get("Parameters.SnapshotUri"))], outputs=["history", "labels", "requests", "catalog"])
    train = {"Name": "Train", "Type": "Training", "Arguments": {
        "TrainingJobName": job_name("train"), "RoleArn": role_arn,
        "AlgorithmSpecification": {"TrainingImage": image_uri, "TrainingInputMode": "File"},
        "HyperParameters": {"candidate_model": get("Parameters.CandidateModel")},
        "InputDataConfig": [{"ChannelName": "history", "ContentType": "application/json", "DataSource": {
            "S3DataSource": {"S3DataType": "S3Prefix", "S3Uri": training_data, "S3DataDistributionType": "FullyReplicated"}}}],
        "OutputDataConfig": {"S3OutputPath": join(root, "/training")},
        "ResourceConfig": {"InstanceCount": 1, "InstanceType": instance_type, "VolumeSizeInGB": 30},
        "StoppingCondition": {"MaxRuntimeInSeconds": 7200}, "EnableNetworkIsolation": True,
    }}
    evaluate = processing("Evaluate", ["--max-wape-ratio", join(get("Parameters.MaxWapeRatio")),
                                       "--min-coverage", join(get("Parameters.MinCoverage"))],
                          inputs=[("model", model_data)], outputs=["evaluation"])
    evaluate["PropertyFiles"] = [{"PropertyFileName": "EvaluationReport", "OutputName": "evaluation", "FilePath": "evaluation.json"}]
    report_uri = get("Steps.Evaluate.ProcessingOutputConfig.Outputs['evaluation'].S3Output.S3Uri")

    def register(status):
        return {"Name": f"Register{status}", "Type": "RegisterModel", "Arguments": {
            "ModelPackageGroupName": model_group, "ModelApprovalStatus": status,
            "ModelPackageDescription": "Chronological replay candidate; inventory costs are simulations.",
            "InferenceSpecification": {"Containers": [{"Image": image_uri, "ModelDataUrl": model_data}],
                "SupportedContentTypes": ["application/jsonlines"], "SupportedResponseMIMETypes": ["application/jsonlines"],
                "SupportedTransformInstanceTypes": [instance_type], "SupportedRealtimeInferenceInstanceTypes": [instance_type]},
            "ModelMetrics": {"ModelQuality": {"Statistics": {"ContentType": "application/json", "S3Uri": join(report_uri, "/evaluation.json")}}},
        }}

    create_model = {"Name": "CreateBatchModel", "Type": "Model", "Arguments": {
        "ModelName": job_name("model"), "ExecutionRoleArn": role_arn, "EnableNetworkIsolation": True,
        "PrimaryContainer": {"ModelPackageName": get("Steps.RegisterApproved.ModelPackageArn")}}}
    transform = {"Name": "DailyForecast", "Type": "Transform", "Arguments": {
        "TransformJobName": job_name("forecast"), "ModelName": get("Steps.CreateBatchModel.ModelName"),
        "MaxConcurrentTransforms": 1, "MaxPayloadInMB": 6, "BatchStrategy": "SingleRecord",
        "TransformInput": {"DataSource": {"S3DataSource": {"S3DataType": "S3Prefix",
            "S3Uri": get("Steps.Prepare.ProcessingOutputConfig.Outputs['requests'].S3Output.S3Uri")}},
            "ContentType": "application/jsonlines", "SplitType": "Line"},
        "TransformOutput": {"S3OutputPath": join(root, "/forecast"), "Accept": "application/jsonlines", "AssembleWith": "Line"},
        "TransformResources": {"InstanceCount": 1, "InstanceType": instance_type}}}
    publish = processing("Publish", ["--run-id", run, "--cutoff", join(cutoff), "--package-arn", get("Steps.RegisterApproved.ModelPackageArn")],
        inputs=[("forecast", get("Steps.DailyForecast.TransformOutput.S3OutputPath")),
                ("labels", get("Steps.Prepare.ProcessingOutputConfig.Outputs['labels'].S3Output.S3Uri")),
                ("catalog", get("Steps.Prepare.ProcessingOutputConfig.Outputs['catalog'].S3Output.S3Uri")),
                ("evaluation", report_uri)])
    reject = processing("Reject", ["--run-id", run, "--cutoff", join(cutoff), "--package-arn", get("Steps.RegisterRejected.ModelPackageArn")],
                        inputs=[("evaluation", report_uri)])
    gate = {"Name": "ReleaseGate", "Type": "Condition", "Arguments": {
        "Conditions": [{"Type": "Equals", "LeftValue": {"Std:JsonGet": {
            "PropertyFile": {"Get": "Steps.Evaluate.PropertyFiles.EvaluationReport"}, "Path": "passed"}}, "RightValue": True}],
        "IfSteps": [register("Approved"), create_model, transform, publish],
        "ElseSteps": [register("Rejected"), reject]}}
    return {"Version": "2020-12-01", "Metadata": {}, "Parameters": [
        {"Name": "RunId", "Type": "String"},
        {"Name": "SnapshotUri", "Type": "String", "DefaultValue": f"s3://{bucket}/raw/dataset.json"},
        {"Name": "Cutoff", "Type": "Integer", "DefaultValue": 196},
        {"Name": "CandidateModel", "Type": "String", "DefaultValue": "seasonal"},
        {"Name": "MaxWapeRatio", "Type": "Float", "DefaultValue": 1.0},
        {"Name": "MinCoverage", "Type": "Float", "DefaultValue": 0.6},
    ], "Steps": [prepare, train, evaluate, gate]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True)
    parser.add_argument("--role-arn", required=True)
    parser.add_argument("--image-uri", required=True)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--table-name", required=True)
    parser.add_argument("--model-group", required=True)
    parser.add_argument("--instance-type", default="ml.m5.xlarge", help="Must have nonzero training, processing and transform quotas")
    parser.add_argument("--output", default="build/pipeline.json")
    args = vars(parser.parse_args())
    path = Path(args.pop("output"))
    definition = build_pipeline(**args)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(definition, indent=2), encoding="utf-8")
    print(path)


if __name__ == "__main__":
    main()
