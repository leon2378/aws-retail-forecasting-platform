"""Rehearse ML job stages locally without creating or publishing AWS resources."""
from __future__ import annotations

import argparse
import copy
from datetime import date, datetime, timedelta, timezone
import hashlib
from importlib.metadata import version
import json
import math
import os
from pathlib import Path
import tarfile
import time
from unittest.mock import patch

from aws import jobs
from aws.serve import infer
from retail_forecast.inventory import compare_policies


SCENARIOS = {
    "default": {},
    "empty_stock_immediate_delivery": {"initial_stock": 0, "lead_time": 0},
    "empty_stock_seven_day_delivery": {"initial_stock": 0, "lead_time": 7},
}


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _finite(value):
    if isinstance(value, dict):
        return all(_finite(item) for item in value.values())
    if isinstance(value, list):
        return all(_finite(item) for item in value)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return math.isfinite(value)
    return True


def _no_training(*args, **kwargs):
    raise RuntimeError("Inference attempted to fit a model")


def _invalid_json_constant(value):
    raise ValueError(f"Dataset contains a non-finite JSON number: {value}")


def _validate_dataset(source):
    """Validate the imported project JSON contract before creating artifacts."""
    _require(isinstance(source, dict), "Dataset must be a JSON object")
    _require(source.get("source") in ("m5", "synthetic"), "Dataset source must be m5 or synthetic")
    _require(isinstance(source.get("source_label"), str) and bool(source["source_label"].strip()),
             "Dataset source_label must be a nonempty string")
    total_days = source.get("total_days")
    _require(isinstance(total_days, int) and not isinstance(total_days, bool) and total_days >= 224,
             "Dataset total_days must be an integer of at least 224")
    _require(isinstance(source.get("start_date"), str), "Dataset start_date must be an ISO date")
    try:
        start = date.fromisoformat(source["start_date"])
        _require(start.isoformat() == source["start_date"], "Dataset start_date must use YYYY-MM-DD")
        start + timedelta(days=total_days - 1)
    except (ValueError, OverflowError) as exc:
        raise ValueError("Dataset dates must use YYYY-MM-DD and fit the supported calendar range") from exc
    _require(isinstance(source.get("series"), list) and bool(source["series"]),
             "Dataset series must be a nonempty array")
    _require("metadata" not in source or isinstance(source["metadata"], dict),
             "Dataset metadata must be an object when supplied")
    for index, series in enumerate(source["series"]):
        _require(isinstance(series, dict), f"Series {index} must be an object")
        for field in ("store_id", "item_id", "name", "category"):
            _require(isinstance(series.get(field), str) and bool(series[field].strip()),
                     f"Series {index} {field} must be a nonempty string")
        _require(isinstance(series.get("values"), list) and len(series["values"]) == total_days,
                 f"Series {index} values must be an array containing exactly total_days observations")


def _validate_forecast(payload, request, cutoff, dates):
    _require(all(payload[key] == request[key] for key in ("store_id", "item_id", "model")),
             "Inference returned a different store, product or model")
    _require(payload["cutoff"] == cutoff, "Inference returned a different replay cutoff")
    _require("_actuals" not in payload, "Inference exposed held-out sales")
    _require(_finite(payload), "Inference returned a non-finite value")
    forecast = payload["forecast"]
    _require(len(forecast) == 28 and [row["date"] for row in forecast] == dates,
             "Inference returned an invalid 28-day horizon")
    _require(all(0 <= row["p10"] <= row["p50"] <= row["p90"] and row["baseline"] >= 0
                 for row in forecast), "Inference returned negative or unordered forecast bands")
    _require(len(payload["backtests"]) == 3
             and all(row["test_days"] == 28 for row in payload["backtests"]),
             "Inference is missing the three chronological backtests")
    _require(payload["training"]["uses_future_labels"] is False,
             "Forecast does not declare its future-label boundary")


def run_rehearsal(dataset_path, output_dir, cutoff=None):
    """Execute Prepare, Train, Evaluate and artifact inference for both candidates.

    Output must be a new or empty directory. All artifacts stay on the local
    filesystem. Rejected candidates are still inferred for comparison only;
    the AWS pipeline would skip their transform and publication stages.
    """
    started = time.monotonic()
    dataset_path, output_dir = Path(dataset_path).resolve(), Path(output_dir).resolve()
    _require(dataset_path.is_file(), "Dataset file does not exist")
    _require(not dataset_path.is_relative_to(output_dir), "Output directory must not contain the source dataset")
    _require(not output_dir.exists() or (output_dir.is_dir() and not any(output_dir.iterdir())),
             "Output directory must be new or empty; existing artifacts are never removed")
    source_bytes = dataset_path.read_bytes()
    source = json.loads(source_bytes.decode("utf-8"), parse_constant=_invalid_json_constant)
    _validate_dataset(source)
    cutoff = source["total_days"] - 28 if cutoff is None else cutoff
    # Validate both candidates before creating output, including the optional ML dependency.
    history, _ = jobs.split_snapshot(source, cutoff, "xgboost")
    from retail_forecast.forecast import _modules
    _modules()

    start = date.fromisoformat(source["start_date"])
    dates = [(start + timedelta(days=cutoff + i)).isoformat() for i in range(28)]
    changed = copy.deepcopy(source)
    for series in changed["series"]:
        series["values"][cutoff:] = [999999] * (len(series["values"]) - cutoff)
    output_dir.mkdir(parents=True, exist_ok=True)
    runs, unique_forecasts = [], {}
    total_forecasts = total_simulations = 0

    for candidate in ("xgboost", "seasonal"):
        run_dir = output_dir / candidate
        input_dir, processing_dir = run_dir / "input", run_dir / "processing"
        jobs.write_json(input_dir / "source/dataset.json", source)
        with patch.object(jobs, "INPUT", input_dir), patch.object(jobs, "OUTPUT", processing_dir):
            jobs.prepare(argparse.Namespace(cutoff=cutoff, model=candidate))
        prepared = jobs.read_json(processing_dir / "history/dataset.json")
        altered, _ = jobs.split_snapshot(changed, cutoff, candidate)
        _require(prepared == altered, "Changing held-out sales changed the training channel")
        _require(all(len(series["values"]) == cutoff for series in prepared["series"]),
                 "Training channel contains observations beyond the cutoff")
        model_dir = run_dir / "trained-model"
        with patch.dict(os.environ, {"SM_CHANNEL_HISTORY": str(processing_dir / "history"),
                                     "SM_MODEL_DIR": str(model_dir)}):
            jobs.train()
        bundle = jobs.load_bundle(model_dir)
        archive_dir = run_dir / "model"
        archive_dir.mkdir()
        with tarfile.open(archive_dir / "model.tar.gz", "w:gz") as archive:
            archive.add(model_dir / "bundle.json", arcname="bundle.json")
        restored = jobs.load_bundle(archive_dir)
        _require(restored == bundle, "Portable model archive did not round-trip")

        # Evaluate loads the compressed model artifact through the same path as the real job.
        with patch.object(jobs, "INPUT", run_dir), patch.object(jobs, "OUTPUT", run_dir):
            jobs.evaluate(argparse.Namespace(max_wape_ratio=1.0, min_coverage=0.6))
        evaluation = jobs.read_json(run_dir / "evaluation/evaluation.json")
        requests_path = processing_dir / "requests/requests.jsonl"
        requests = [json.loads(line) for line in requests_path.read_text(encoding="utf-8").splitlines()]
        models = sorted({candidate, "seasonal"})
        expected = {(series["store_id"], series["item_id"], model)
                    for series in source["series"] for model in models}
        _require({(r["store_id"], r["item_id"], r["model"]) for r in requests} == expected
                 and len(requests) == len(expected), "Prepare emitted an incomplete or duplicate request batch")
        labels = jobs.read_json(processing_dir / "labels/labels.json")
        transform_dir = run_dir / "transform"
        transform_dir.mkdir()
        simulations = []
        with (transform_dir / "requests.jsonl.out").open("w", encoding="utf-8", newline="\n") as stream:
            # Guard both the public fitting entry point and its underlying model fit.
            with patch("retail_forecast.forecast.train_artifact", side_effect=_no_training), \
                    patch("retail_forecast.forecast._fit", side_effect=_no_training):
                for request in requests:
                    payload = infer(restored, request)
                    _validate_forecast(payload, request, cutoff, dates)
                    stream.write(json.dumps(payload, allow_nan=False, separators=(",", ":")) + "\n")
                    identity = (request["store_id"], request["item_id"], request["model"])
                    if identity in unique_forecasts:
                        _require(unique_forecasts[identity] == payload, "Baseline inference changed between candidate runs")
                    unique_forecasts[identity] = payload
                    actuals = labels[jobs.series_key(*identity[:2])]
                    _require(actuals == next(s["values"][cutoff:cutoff + 28] for s in source["series"]
                                            if (s["store_id"], s["item_id"]) == identity[:2]),
                             "Scoring labels do not match the held-out source window")
                    for scenario, assumptions in SCENARIOS.items():
                        result = compare_policies(actuals, payload["forecast"], assumptions)
                        _require(_finite(result) and len(result["policies"]) == 3,
                                 "Inventory comparison returned invalid metrics")
                        _require(all(len(policy["daily"]) == 28 and 0 <= policy["fill_rate"] <= 1
                                     and policy["total_cost"] >= 0 for policy in result["policies"]),
                                 "Inventory simulation returned invalid daily rows or totals")
                        simulations.append({**request, "scenario": scenario,
                                            "assumptions": result["assumptions"],
                                            "policies": [{k: policy[k] for k in
                                                          ("id", "fill_rate", "total_cost", "lost_units",
                                                           "units_ordered", "ending_inventory", "outstanding_units")}
                                                         for policy in result["policies"]]})
                        total_simulations += len(result["policies"])
                    total_forecasts += 1
        jobs.write_json(run_dir / "simulations.json", simulations)
        runs.append({"candidate": candidate, "release_gate": evaluation,
                     "forecast_count": len(requests), "models_inferred": models,
                     "model_archive": str((archive_dir / "model.tar.gz").relative_to(output_dir)),
                     "requests": str(requests_path.relative_to(output_dir)),
                     "forecasts": str((transform_dir / "requests.jsonl.out").relative_to(output_dir)),
                     "comparison_only": not evaluation["passed"]})

    report = {
        "verified_at": datetime.now(timezone.utc).isoformat(), "mode": "local_job_rehearsal",
        "source": source["source"], "source_label": source["source_label"],
        "dataset_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "series_count": len(source["series"]), "total_days": source["total_days"], "cutoff": cutoff,
        "forecast_start": dates[0], "forecast_end": dates[-1],
        "backtest_points_per_candidate": len(source["series"]) * 84,
        "forecast_count": total_forecasts, "unique_forecast_count": len(unique_forecasts),
        "policy_simulation_count": total_simulations, "inventory_scenarios": SCENARIOS,
        "checks_passed": True, "runs": runs,
        "versions": {name: version(name) for name in ("numpy", "xgboost")},
        "elapsed_seconds": round(time.monotonic() - started, 2),
        "aws_execution": {"model_registry": False, "batch_transform": False, "publication": False},
        "limitations": [
            "Local job functions and portable artifacts only; no AWS jobs, model registration or publication executed.",
            "Source classifications and labels identify the supplied imported input; this rehearsal does not verify dataset authenticity.",
            "Rejected candidates are inferred locally for inspection; the AWS release gate would skip that batch.",
            "Three chronological 28-day folds use pooled WAPE, not official hierarchical M5 WRMSSE.",
            "Bands are approximate and have no guaranteed coverage; all inventory savings are simulation results.",
            "Passing baseline parity verifies the release gate, not improved forecasting accuracy.",
        ],
    }
    jobs.write_json(output_dir / "report.json", report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=Path("data/dataset.json"),
                        help="Normalized project dataset JSON produced by import-m5 or the synthetic generator")
    parser.add_argument("--output", type=Path, default=Path("build/local-rehearsal"),
                        help="New or empty local artifact directory; never removed automatically")
    parser.add_argument("--cutoff", type=int, help="Observed historical days; default leaves a 28-day holdout")
    args = parser.parse_args(argv)
    try:
        report = run_rehearsal(args.dataset, args.output, args.cutoff)
    except (ValueError, OSError, RuntimeError) as exc:
        parser.error(str(exc))
    for run in report["runs"]:
        gate = run["release_gate"]
        print(f"{run['candidate']}: {'PASS' if gate['passed'] else 'REJECT'} "
              f"(WAPE={gate['wape']}, baseline={gate['baseline_wape']}, coverage={gate['coverage']})")
    print(f"Validated {report['unique_forecast_count']} unique 28-day forecasts and "
          f"{report['policy_simulation_count']} policy simulations locally. No AWS resources used.")
    print(f"Report: {(args.output / 'report.json').resolve()}")


if __name__ == "__main__":
    main()
