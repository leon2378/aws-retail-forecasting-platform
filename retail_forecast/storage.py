"""Local development and DynamoDB repositories share a materialized API schema."""

from __future__ import annotations

from collections import OrderedDict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import importlib.util
import json
import os
from pathlib import Path
from threading import RLock
from uuid import uuid4

from .data import demo_dataset
from .forecast import analyze_series
from .operations import LocalOperations

MIN_CUTOFF = 196
HORIZON = 28


def build_catalog(dataset, mode="local", available_models=None):
    if available_models is None:
        available_models = ["seasonal"]
        if importlib.util.find_spec("xgboost") is not None:
            available_models.append("xgboost")
    return {
        "source": dataset["source"], "source_label": dataset["source_label"],
        "start_date": dataset["start_date"], "total_days": dataset["total_days"],
        "default_cutoff": dataset["total_days"] - HORIZON,
        "min_cutoff": MIN_CUTOFF,
        "stores": [{"id": store, "name": store.replace("_", " · ")}
                   for store in sorted({s["store_id"] for s in dataset["series"]})],
        "products": [{k: s[k] for k in ("store_id", "item_id", "name", "category")}
                     for s in dataset["series"]],
        "models": [{"id": model, "name": name, "available": model in available_models}
                   for model, name in [("seasonal", "Seasonal baseline"), ("xgboost", "XGBoost")]],
        "mode": mode,
    }


def build_forecast_payload(dataset, store, item, cutoff, model="seasonal", analysis=None):
    if isinstance(cutoff, bool) or not isinstance(cutoff, int):
        raise ValueError("cutoff must be a whole number of observed days")
    if not MIN_CUTOFF <= cutoff <= dataset["total_days"]:
        raise ValueError(f"cutoff must be between {MIN_CUTOFF} and {dataset['total_days']}")
    series = next((s for s in dataset["series"] if s["store_id"] == store and s["item_id"] == item), None)
    if series is None:
        raise LookupError("This store/product pair is not in the dataset")
    analysis = analysis if analysis is not None else analyze_series(series, dataset["start_date"], cutoff, model)
    mean_recent = sum(series["values"][cutoff - 28:cutoff]) / 28
    total = sum(p["p50"] for p in analysis["forecast"])
    as_of = date.fromisoformat(dataset["start_date"]) + timedelta(days=cutoff - 1)
    max_replay = dataset["total_days"] - HORIZON
    return {
        **analysis, "store_id": store, "item_id": item, "source": dataset["source"],
        "source_label": dataset["source_label"], "cutoff": cutoff, "as_of": as_of.isoformat(),
        "summary": {"total_demand": round(total, 2), "average_daily": round(total / 28, 2),
                    "peak_demand": max(p["p50"] for p in analysis["forecast"]),
                    "change_percent": (total / 28 - mean_recent) / mean_recent if mean_recent else None},
        "replay": {"can_advance": cutoff < max_replay, "next_cutoff": min(cutoff + 1, max_replay),
                   "max_cutoff": max_replay, "min_cutoff": MIN_CUTOFF},
        "_actuals": series["values"][cutoff:cutoff + HORIZON],
    }


class LocalRepository:
    mode = "local"

    def __init__(self, data_dir="data", dataset=None, approve_contract=False):
        self.data_dir = Path(data_dir)
        dataset_path = self.data_dir / "dataset.json"
        self._cache = OrderedDict()
        self._lock = RLock()
        self._operations = LocalOperations(self.data_dir / "operations", build_forecast_payload)
        if dataset is not None:
            self.dataset = self._operations.initialize(dataset, approve_contract=approve_contract)
        elif dataset_path.exists():
            self.dataset = self._operations.initialize(source_path=dataset_path, approve_contract=approve_contract)
        else:
            # Restart an explicitly supplied dataset's verified checkpoint even
            # when no importer-owned dataset.json was written by the caller.
            accepted = self._operations.active()
            candidate = accepted["dataset"] if accepted else demo_dataset()
            self.dataset = self._operations.initialize(candidate, approve_contract=approve_contract)
        self._dataset_hash = self._operations.active()["manifest"]["dataset_hash"]

    @classmethod
    def import_snapshot(cls, path, dataset):
        """Explicit CLI import: gate, checkpoint and atomically replace its source."""
        repository = cls.__new__(cls)
        repository.data_dir = Path(path).parent
        repository._cache = OrderedDict()
        repository._lock = RLock()
        repository._operations = LocalOperations(repository.data_dir / "operations", build_forecast_payload)
        repository.dataset = repository._operations.import_snapshot(path, dataset)
        repository._dataset_hash = repository._operations.active()["manifest"]["dataset_hash"]
        return repository

    def _sync_dataset(self):
        active = self._operations.active()
        if active and active["manifest"]["dataset_hash"] != self._dataset_hash:
            self.dataset = active["dataset"]
            self._dataset_hash = active["manifest"]["dataset_hash"]
            self._cache.clear()
        return active

    def catalog(self):
        with self._lock:
            self._sync_dataset()
            return build_catalog(self.dataset)

    def forecast(self, store, item, cutoff, model):
        key = (store, item, cutoff, model)
        with self._lock:
            active = self._sync_dataset()
            if active and model == "seasonal" and cutoff == active["manifest"]["cutoff"]:
                payload = next((row for row in active["forecasts"]
                                if row["store_id"] == store and row["item_id"] == item), None)
                if payload is None:
                    raise LookupError("This store/product pair is not in the dataset")
                series = next(row for row in active["dataset"]["series"]
                              if row["store_id"] == store and row["item_id"] == item)
                return {**payload, "_actuals": series["values"][cutoff:cutoff + HORIZON]}
            if key not in self._cache:
                self._cache[key] = build_forecast_payload(self.dataset, *key)
                if len(self._cache) > 64:
                    self._cache.popitem(last=False)
            self._cache.move_to_end(key)
            return self._cache[key]

    def operations(self):
        return self._operations.status()

    def recovery_drill(self, request_token=None):
        with self._lock:
            self._sync_dataset()
            return self._operations.run_drill(self.dataset, request_token)

    def releases(self):
        with self._lock:
            path = self.data_dir / "releases.json"
            return {"releases": json.loads(path.read_text(encoding="utf-8")) if path.exists() else []}

    def demo_releases(self):
        """Exercise a real comparison gate with a deliberately bad model and a baseline clone.

        This does not promote the application's forecast model or assert ML improvement.
        """
        with self._lock:
            self._sync_dataset()
            series = self.dataset["series"][0]
            cutoff = self.dataset["total_days"] - HORIZON
            result = self.forecast(series["store_id"], series["item_id"], cutoff, "seasonal")
            actuals = result["_actuals"]
            baseline = [p["baseline"] for p in result["forecast"]]
            denominator = sum(actuals)
            if denominator == 0:
                raise ValueError("Release demo requires a series with nonzero holdout sales")
            baseline_wape = sum(abs(a - p) for a, p in zip(actuals, baseline)) / denominator
            # The added offset guarantees this intentionally broken candidate fails the gate.
            broken = [p + max(actuals) + denominator + 1 for p in baseline]
            broken_wape = sum(abs(a - p) for a, p in zip(actuals, broken)) / denominator
            threshold = baseline_wape + 1e-9
            now = datetime.now(timezone.utc).isoformat()
            new = []
            for model, score, reason in [
                ("Deliberately degraded baseline", broken_wape, "Intentional bad-candidate exercise: holdout WAPE exceeds baseline."),
                ("Baseline clone", baseline_wape, "Baseline-parity gate passes. This demonstrates the workflow, not forecast improvement."),
            ]:
                new.append({"id": str(uuid4()), "model": model,
                            "status": "Approved" if score <= threshold else "Rejected", "created_at": now,
                            "reason": reason, "synthetic_demo": self.dataset["source"] == "synthetic", "workflow_demo": True,
                            "source": self.dataset["source"],
                            "metrics": {"wape": score, "baseline_wape": baseline_wape,
                                        "threshold": threshold, "cutoff": cutoff}})
            history = (self.releases()["releases"] + new)[-100:]
            self.data_dir.mkdir(parents=True, exist_ok=True)
            path = self.data_dir / "releases.json"
            temp = self.data_dir / "releases.json.tmp"
            temp.write_text(json.dumps(history, indent=2, allow_nan=False), encoding="utf-8")
            os.replace(temp, path)
            return {"releases": history}


def _plain(value):
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


class DynamoRepository:
    mode = "aws"

    def __init__(self, table_name=None, table=None):
        if table is None:
            import boto3
            table = boto3.resource("dynamodb").Table(table_name or os.environ["RESULTS_TABLE"])
        self.table = table

    def _get(self, pk, sk):
        item = self.table.get_item(Key={"pk": pk, "sk": sk}, ConsistentRead=True).get("Item")
        if not item:
            raise LookupError("No published result exists yet for this selection")
        return _plain(item["payload"])

    def catalog(self):
        return {**self._get("CATALOG", "META"), "mode": "aws"}

    def forecast(self, store, item, cutoff, model):
        marker = self.table.get_item(Key={"pk": "COMPLETED", "sk": f"{cutoff:09d}"}, ConsistentRead=True).get("Item")
        if not marker or model not in marker.get("models", []):
            raise LookupError("This replay day/model has not completed publication")
        payload = self._get(f"SERIES#{store}#{item}", f"FORECAST#{cutoff:09d}#{model}")
        if payload.get("_run_id") != marker.get("run_id") or not marker.get("run_id"):
            raise LookupError("Forecast row does not belong to the completed publication")
        catalog = self.catalog()
        latest = catalog["default_cutoff"]
        return {**payload, "replay": {"can_advance": cutoff < latest, "next_cutoff": min(cutoff + 1, latest),
                                      "max_cutoff": latest, "min_cutoff": catalog.get("min_cutoff", MIN_CUTOFF)}}

    def releases(self):
        # Return latest 100 records; unique sortable IDs are created by the pipeline publisher.
        response = self.table.query(KeyConditionExpression="pk = :pk",
                                    ExpressionAttributeValues={":pk": "RELEASES"},
                                    ScanIndexForward=False, Limit=100, ConsistentRead=True)
        return {"releases": [_plain(row["payload"]) for row in response.get("Items", [])]}

    def demo_releases(self):
        raise PermissionError("Release demonstrations run locally; AWS releases are controlled by the ML pipeline")

    def recovery_drill(self, request_token=None):
        raise PermissionError("Recovery drills run in an isolated local workspace; AWS operations are read-only")

    def operations(self):
        """Read only existing publication/replay records; do not poll job services."""
        scope = "AWS published metadata"
        try:
            catalog = self.catalog()
        except LookupError:
            catalog = {}
        replay = _plain(self.table.get_item(Key={"pk": "REPLAY", "sk": "STATE"},
                                           ConsistentRead=True).get("Item", {}))
        cutoff = catalog.get("default_cutoff")
        marker = _plain(self.table.get_item(Key={"pk": "COMPLETED", "sk": f"{cutoff:09d}"},
                                           ConsistentRead=True).get("Item", {})) if isinstance(cutoff, int) else {}
        releases = self.releases()["releases"]
        expected = replay.get("pending_cutoff", replay.get("cutoff", cutoff))
        lag = max(0, expected - cutoff) if isinstance(expected, int) and isinstance(cutoff, int) else None
        committed = bool(marker.get("run_id") and marker.get("models"))
        if catalog.get("published_run_id") and catalog["published_run_id"] != marker.get("run_id"):
            committed = False
        quality = replay.get("latest_quality")
        if not isinstance(quality, dict):
            quality = {"passed": None, "checked_at": None, "source": catalog.get("source"),
                       "cutoff": None, "series_count": None, "snapshot_sha256": None,
                       "checks": [], "issues": []}
        alerts = []
        if catalog and not committed:
            alerts.append({"severity": "error", "message": "Catalog metadata has no matching completed publication marker."})
        if quality.get("passed") is False:
            alerts.append({"severity": "error", "message": "The latest source failed data quality checks; previously completed publications remain available."})
        elif committed and quality.get("passed") is None:
            alerts.append({"severity": "warning", "message": "Data quality results are not recorded for this publication."})
        if lag:
            alerts.append({"severity": "warning", "message": f"The active forecast is {lag} historical replay day(s) behind the expected cutoff."})
        runs = [{"id": row.get("id"), "type": "model_release",
                 "status": "completed" if row.get("status") == "Approved" else "rejected" if row.get("status") == "Rejected" else "unknown",
                 "stage": "release_gate", "cutoff": row.get("metrics", {}).get("cutoff"),
                 "started_at": None, "finished_at": row.get("created_at"),
                 "reason": row.get("reason"), "scope": scope} for row in releases[:20]]
        if replay.get("run_id"):
            runs.insert(0, {"id": replay["run_id"], "type": "replay", "status": "pending",
                "stage": "Pipeline execution state not recorded", "cutoff": replay.get("pending_cutoff"),
                "started_at": replay.get("started_at"), "finished_at": None,
                "reason": "A replay lock exists; its actual job state is unknown to this dashboard.", "scope": scope})
        matching = next((row for row in releases if row.get("id") == marker.get("run_id")), {})
        active_model = next((model for model in marker.get("models", []) if model != "seasonal"),
                            marker.get("models", [None])[0] if marker.get("models") else None)
        return {"mode": "aws", "scope": scope, "source": catalog.get("source"),
            "source_label": catalog.get("source_label"),
            "summary": {"status": "awaiting_publication" if not catalog else "attention" if alerts else "healthy",
                "last_success_at": catalog.get("published_at", matching.get("created_at")) if committed else None,
                "active_cutoff": cutoff if committed else None,
                "active_as_of": (date.fromisoformat(catalog["start_date"]) + timedelta(days=cutoff - 1)).isoformat()
                    if committed and catalog.get("start_date") and isinstance(cutoff, int) else None,
                "expected_cutoff": expected, "replay_lag_days": lag, "active_model": active_model if committed else None,
                "active_version": marker.get("run_id") if committed else None,
                "snapshot_hash": catalog.get("snapshot_sha256") if committed else None,
                "series_count": len(catalog.get("products", [])) if committed else 0},
            "quality": quality, "runs": runs[:20],
            "events": [{"at": row.get("created_at"), "type": "model_release", "message": row.get("reason"),
                        "run_id": row.get("id")} for row in releases[:30]],
            "latest_drill": None, "capabilities": {"can_run_drill": False}, "alerts": alerts,
            "limitations": ["AWS status reads recorded DynamoDB metadata only; running SageMaker job state is unknown unless recorded.",
                "Publication markers guard forecast visibility; this view does not re-download or hash S3 artifacts.",
                "Freshness follows historical replay cutoffs. No latency or uptime telemetry is collected."]}
