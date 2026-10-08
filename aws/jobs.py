"""Container commands for preparation, training, evaluation and publication."""
import argparse
import copy
import json
import math
import os
import tarfile
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

INPUT = Path("/opt/ml/processing/input")
OUTPUT = Path("/opt/ml/processing/output")


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, allow_nan=False, separators=(",", ":")), encoding="utf-8")


def split_snapshot(dataset, cutoff, candidate="seasonal"):
    """Return disjoint training history and scoring labels; never pad future data."""
    if candidate not in {"seasonal", "xgboost"}:
        raise ValueError("Candidate model must be seasonal or xgboost")
    if not isinstance(cutoff, int) or isinstance(cutoff, bool) or not 196 <= cutoff <= dataset["total_days"] - 28:
        raise ValueError("Replay cutoff must leave 196 observed and 28 scoring days")
    history = copy.deepcopy(dataset)
    labels = {}
    for series in history["series"]:
        if len(series["values"]) < cutoff + 28:
            raise ValueError("Series is shorter than the replay window")
        key = series_key(series["store_id"], series["item_id"])
        if key in labels:
            raise ValueError("Duplicate store and item")
        labels[key] = series["values"][cutoff:cutoff + 28]
        series["values"] = series["values"][:cutoff]
    history["cutoff"] = cutoff
    history["candidate_model"] = candidate
    return history, labels


def series_key(store, item):
    return f"{store}#{item}"


def prepare(args):
    from retail_forecast.storage import build_catalog
    source = next((INPUT / "source").glob("*.json"))
    dataset = read_json(source)
    history, labels = split_snapshot(dataset, args.cutoff, args.model)
    models = sorted({"seasonal", args.model})
    catalog = build_catalog(dataset, mode="aws", available_models=models)
    catalog["default_cutoff"] = args.cutoff
    write_json(OUTPUT / "history/dataset.json", history)
    write_json(OUTPUT / "labels/labels.json", labels)
    write_json(OUTPUT / "catalog/catalog.json", catalog)
    requests = [{"store_id": s["store_id"], "item_id": s["item_id"], "model": model}
                for s in dataset["series"] for model in models]
    request_path = OUTPUT / "requests/requests.jsonl"
    request_path.parent.mkdir(parents=True, exist_ok=True)
    request_path.write_text("\n".join(json.dumps(r) for r in requests) + "\n", encoding="utf-8")


def train(args=None):
    from retail_forecast.forecast import train_artifact
    history = read_json(Path(os.environ.get("SM_CHANNEL_HISTORY", "/opt/ml/input/data/history")) / "dataset.json")
    cutoff = history["cutoff"]
    artifacts = {}
    for series in history["series"]:
        if len(series["values"]) != cutoff:
            raise ValueError("Training channel must contain exactly cutoff observations")
        key = series_key(series["store_id"], series["item_id"])
        artifacts[key] = {model: train_artifact(series, history["start_date"], cutoff, model)
                          for model in sorted({"seasonal", history["candidate_model"]})}
    write_json(Path(os.environ.get("SM_MODEL_DIR", "/opt/ml/model")) / "bundle.json",
               {"dataset": history, "artifacts": artifacts})


def load_bundle(directory):
    directory = Path(directory)
    if (directory / "bundle.json").exists():
        return read_json(directory / "bundle.json")
    # Read only the one expected member; never extract arbitrary archive paths.
    with tarfile.open(next(directory.glob("*.tar.gz")), "r:gz") as archive:
        member = next(m for m in archive.getmembers() if m.name in {"bundle.json", "./bundle.json"})
        if not member.isfile() or member.size > 256_000_000:
            raise ValueError("Invalid model bundle")
        return json.load(archive.extractfile(member))


def evaluate_bundle(bundle, max_wape_ratio=1.0, min_coverage=0.6):
    """Pooled WAPE across scored windows; final future labels remain unavailable."""
    from retail_forecast.forecast import predict_artifact
    if not math.isfinite(max_wape_ratio) or max_wape_ratio <= 0 or not 0 <= min_coverage <= 1:
        raise ValueError("Invalid release thresholds")
    dataset = bundle["dataset"]
    candidate = dataset["candidate_model"]
    total_weight = weighted_wape = weighted_baseline = coverage_sum = 0.0
    series_count = 0
    for series in dataset["series"]:
        artifact = bundle["artifacts"][series_key(series["store_id"], series["item_id"])][candidate]
        # Prediction is pure inference from the fitted artifact, no refitting.
        metrics = predict_artifact(artifact)["metrics"]
        weight = sum(series["values"][dataset["cutoff"] - 84:dataset["cutoff"]])
        coverage = metrics["coverage"]
        if not isinstance(coverage, (int, float)) or not math.isfinite(coverage):
            raise ValueError("Non-finite backtest coverage")
        if weight:
            for field in ["wape", "baseline_wape"]:
                if not isinstance(metrics[field], (int, float)) or not math.isfinite(metrics[field]):
                    raise ValueError("Non-finite backtest WAPE")
            weighted_wape += metrics["wape"] * weight
            weighted_baseline += metrics["baseline_wape"] * weight
            total_weight += weight
        coverage_sum += coverage
        series_count += 1
    if not series_count:
        raise ValueError("No series to evaluate")
    wape = weighted_wape / total_weight if total_weight else None
    baseline = weighted_baseline / total_weight if total_weight else None
    coverage = coverage_sum / series_count
    passed = wape is not None and wape <= baseline * max_wape_ratio + 1e-12 and coverage >= min_coverage
    reason = ("Candidate met chronological backtest WAPE and coverage gates." if passed else
              "Candidate failed WAPE/coverage gate or all scored demand was zero; cursor not advanced.")
    return {"passed": passed, "model": candidate, "wape": wape, "baseline_wape": baseline,
            "coverage": coverage, "max_wape_ratio": max_wape_ratio, "min_coverage": min_coverage,
            "series_count": series_count, "source": dataset["source"], "reason": reason}


def evaluate(args):
    report = evaluate_bundle(load_bundle(INPUT / "model"), args.max_wape_ratio, args.min_coverage)
    write_json(OUTPUT / "evaluation/evaluation.json", report)


def dynamo(value):
    from boto3.dynamodb.types import TypeSerializer
    # Convert JSON floats to Decimal before Dynamo serialization.
    safe = json.loads(json.dumps(value, allow_nan=False), parse_float=Decimal)
    serializer = TypeSerializer()
    return {key: serializer.serialize(item) for key, item in safe.items()}


def release_payload(args, report, status, created_at=None):
    return {"id": args.run_id, "model": report["model"], "status": status,
            "created_at": created_at or datetime.now(timezone.utc).isoformat(), "reason": report["reason"],
            "metrics": {k: report[k] for k in ["wape", "baseline_wape", "coverage"]},
            "synthetic_demo": report["source"] == "synthetic", "model_package_arn": args.package_arn,
            "cutoff": args.cutoff}


def publish(args):
    import boto3
    from botocore.exceptions import ClientError
    table_name = os.environ["RESULTS_TABLE"]
    table = boto3.resource("dynamodb").Table(table_name)
    marker_key = {"pk": "COMPLETED", "sk": f"{args.cutoff:09d}"}
    completed = table.get_item(Key=marker_key, ConsistentRead=True).get("Item")
    if completed:
        if completed.get("run_id") != args.run_id:
            raise ValueError("This cutoff was already published by another run")
        return  # Retrying after a lost commit response cannot rewrite snapshots.
    state = table.get_item(Key={"pk": "REPLAY", "sk": "STATE"}, ConsistentRead=True).get("Item", {})
    if state.get("run_id") != args.run_id or state.get("pending_cutoff") != args.cutoff:
        raise ValueError("This run does not own the pending replay cutoff")
    report = read_json(INPUT / "evaluation/evaluation.json")
    if not report["passed"]:
        raise ValueError("Refusing to publish a rejected candidate")
    labels = read_json(INPUT / "labels/labels.json")
    catalog = read_json(INPUT / "catalog/catalog.json")
    previous = table.get_item(Key={"pk": "CATALOG", "sk": "META"}, ConsistentRead=True).get("Item", {})
    catalog["min_cutoff"] = previous.get("payload", {}).get("min_cutoff", args.cutoff)
    if catalog["default_cutoff"] != args.cutoff:
        raise ValueError("Catalog cutoff does not match the pending run")
    expected = {(s["store_id"], s["item_id"], m["id"]) for s in catalog["products"]
                for m in catalog["models"] if m["available"]}
    seen = set()
    with table.batch_writer() as batch:
        for path in (INPUT / "forecast").rglob("*.out"):
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                payload = json.loads(line, parse_float=Decimal)
                identity = (payload["store_id"], payload["item_id"], payload["model"])
                if identity not in expected or identity in seen or payload["cutoff"] != args.cutoff or len(payload["forecast"]) != 28:
                    raise ValueError("Unexpected, duplicate or incomplete prediction")
                seen.add(identity)
                payload["_actuals"] = labels[series_key(*identity[:2])]
                payload["_run_id"] = args.run_id
                batch.put_item(Item={"pk": f"SERIES#{identity[0]}#{identity[1]}",
                                     "sk": f"FORECAST#{args.cutoff:09d}#{identity[2]}", "payload": payload})
    if seen != expected:
        raise ValueError("Forecast batch is incomplete; leaving catalog and cursor unchanged")
    release = release_payload(args, report, "approved", state["started_at"])
    transaction = [
        {"Put": {"TableName": table_name, "Item": dynamo({"pk": "CATALOG", "sk": "META", "payload": catalog})}},
        {"Put": {"TableName": table_name, "Item": dynamo({"pk": "COMPLETED", "sk": f"{args.cutoff:09d}",
            "models": [m["id"] for m in catalog["models"] if m["available"]], "run_id": args.run_id}),
            "ConditionExpression": "attribute_not_exists(pk)"}},
        {"Put": {"TableName": table_name, "Item": dynamo({"pk": "RELEASES", "sk": f"{release['created_at']}#{args.run_id}",
            "payload": release})}},
        {"Update": {"TableName": table_name, "Key": dynamo({"pk": "REPLAY", "sk": "STATE"}),
            "UpdateExpression": "SET cutoff = :cutoff REMOVE run_id, pending_cutoff, execution_arn, started_at",
            "ConditionExpression": "run_id = :run AND pending_cutoff = :cutoff",
            "ExpressionAttributeValues": dynamo({":run": args.run_id, ":cutoff": args.cutoff})}},
    ]
    try:
        boto3.client("dynamodb").transact_write_items(TransactItems=transaction)
    except ClientError:
        # A response lost after commit is safe to retry without recommitting.
        completed = table.get_item(Key={"pk": "COMPLETED", "sk": f"{args.cutoff:09d}"}, ConsistentRead=True).get("Item", {})
        if completed.get("run_id") != args.run_id:
            raise


def reject(args):
    import boto3
    table_name = os.environ["RESULTS_TABLE"]
    report = read_json(INPUT / "evaluation/evaluation.json")
    if report["passed"]:
        raise ValueError("Cannot record rejection of a passing report")
    table = boto3.resource("dynamodb").Table(table_name)
    state = table.get_item(Key={"pk": "REPLAY", "sk": "STATE"}, ConsistentRead=True).get("Item", {})
    if state.get("run_id") != args.run_id or state.get("pending_cutoff") != args.cutoff:
        raise ValueError("This run does not own the pending replay cutoff")
    release = release_payload(args, report, "rejected", state["started_at"])
    table.put_item(Item=json.loads(json.dumps({"pk": "RELEASES", "sk": f"{release['created_at']}#{args.run_id}",
        "payload": release}), parse_float=Decimal))
    # Keep the lock until SageMaker reports a terminal execution: the next daily
    # trigger clears it. Clearing here could allow overlapping executions.


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=["prepare", "train", "evaluate", "publish", "reject", "serve"])
    parser.add_argument("--cutoff", type=int)
    parser.add_argument("--model", default="seasonal")
    parser.add_argument("--max-wape-ratio", type=float, default=1.0)
    parser.add_argument("--min-coverage", type=float, default=0.6)
    parser.add_argument("--run-id")
    parser.add_argument("--package-arn")
    args = parser.parse_args()
    if args.stage == "serve":
        from aws.serve import serve
        serve()
    else:
        globals()[args.stage](args)


if __name__ == "__main__":
    main()
