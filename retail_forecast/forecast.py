"""Leakage-resistant 28-day forecasting, evaluation and portable model artifacts."""
from __future__ import annotations

import base64
from datetime import date, timedelta
import math
from numbers import Real
import statistics

HORIZON = 28
MIN_CUTOFF = 196
FEATURES = ["lag_1", "lag_7", "lag_14", "lag_28", "mean_7", "mean_28", "std_28",
            "weekday_sin", "weekday_cos", "year_sin", "year_cos"]


def _number(value, name, minimum=0):
    # Bound integers before float conversion: isfinite(10**1000) itself overflows.
    if (isinstance(value, bool) or not isinstance(value, Real)
            or not minimum <= value <= 100_000_000 or not math.isfinite(value)):
        raise ValueError(f"{name} must be a finite number between {minimum} and 100000000")
    return float(value)


def _modules():
    try:
        import numpy as np
        import xgboost as xgb
    except ImportError as exc:
        raise ValueError("XGBoost is not installed. Install the project's [ml] extra: pip install -e '.[ml]'") from exc
    return np, xgb


def _features(history, when):
    tail = history[-28:]
    weekday = 2 * math.pi * when.weekday() / 7
    year = 2 * math.pi * (when.timetuple().tm_yday - 1) / 365.25
    return [history[-1], history[-7], history[-14], history[-28],
            statistics.fmean(history[-7:]), statistics.fmean(tail), statistics.pstdev(tail),
            math.sin(weekday), math.cos(weekday), math.sin(year), math.cos(year)]


def _fit(values, start, model):
    if model == "seasonal":
        return {"type": "seasonal", "weekly": [statistics.fmean(values[-28 + i::7]) for i in range(7)]}
    np, xgb = _modules()
    if not any(values):
        return {"type": "constant", "value": 0.0, "reason": "All observed sales are zero"}
    rows = [_features(values[:t], start + timedelta(days=t)) for t in range(28, len(values))]
    labels = values[28:]
    matrix = xgb.DMatrix(np.asarray(rows, dtype=np.float32), label=np.asarray(labels, dtype=np.float32),
                         feature_names=FEATURES)
    params = {"objective": "count:poisson", "tree_method": "hist", "max_depth": 3, "eta": 0.06,
              "min_child_weight": 5, "lambda": 4, "subsample": 1, "colsample_bytree": 1,
              "seed": 42, "nthread": 1, "verbosity": 0,
              "base_score": max(0.01, statistics.fmean(labels))}
    booster = xgb.train(params, matrix, num_boost_round=100)
    return {"type": "xgboost", "format": "ubj", "booster": base64.b64encode(booster.save_raw(raw_format="ubj")).decode("ascii"),
            "params": params, "num_boost_round": 100, "xgboost_version": xgb.__version__}


def _predict(state, tail, origin_date):
    if state["type"] == "seasonal":
        return [float(state["weekly"][i % 7]) for i in range(HORIZON)]
    if state["type"] == "constant":
        return [float(state["value"])] * HORIZON
    np, xgb = _modules()
    booster = xgb.Booster(params={"nthread": 1})
    booster.load_model(bytearray(base64.b64decode(state["booster"])))
    history, predictions = list(tail), []
    for step in range(HORIZON):
        features = _features(history, origin_date + timedelta(days=step))
        matrix = xgb.DMatrix(np.asarray([features], dtype=np.float32), feature_names=FEATURES)
        value = max(0.0, float(booster.predict(matrix)[0]))
        if not math.isfinite(value):
            raise ValueError("Model produced a nonfinite forecast")
        predictions.append(value)
        history.append(value)
    return predictions


def _quantile(values, fraction):
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower, upper = math.floor(position), math.ceil(position)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _bands(predictions, calibration):
    lower = min(0.0, calibration["lower_offset"])
    upper = max(0.0, calibration["upper_offset"])
    return [(max(0.0, value + lower), value, max(value, value + upper)) for value in predictions]


def _metrics(actuals, predictions, baseline, bands):
    total = sum(actuals)
    absolute = sum(abs(actual - predicted) for actual, predicted in zip(actuals, predictions))
    return {"wape": absolute / total if total else None,
            "mae": absolute / len(actuals),
            "bias": sum(predicted - actual for actual, predicted in zip(actuals, predictions)) / total if total else None,
            "coverage": sum(low <= actual <= high for actual, (low, _, high) in zip(actuals, bands)) / len(actuals),
            "baseline_wape": sum(abs(actual - predicted) for actual, predicted in zip(actuals, baseline)) / total if total else None}


def _window(start, origin):
    return {"observed_days": origin, "train_end_date": (start + timedelta(days=origin - 1)).isoformat(),
            "test_start_date": (start + timedelta(days=origin)).isoformat(),
            "test_end_date": (start + timedelta(days=origin + HORIZON - 1)).isoformat()}


def train_artifact(series, start_date, cutoff, model="seasonal") -> dict:
    """Fit a serializable model artifact; final horizon inference is a separate step.

    Two calibration windows precede three disjoint scored test windows. Models
    are refit using only each origin's preceding observations. Scored windows
    never contribute residuals to calibration. Hyperparameters are fixed.
    """
    if model not in ("seasonal", "xgboost"):
        raise ValueError("model must be 'seasonal' or 'xgboost'")
    if not isinstance(series, dict) or not isinstance(series.get("values"), (list, tuple)):
        raise ValueError("series.values must be a list of observed sales")
    raw_values = series["values"]
    if isinstance(cutoff, bool) or not isinstance(cutoff, int) or not MIN_CUTOFF <= cutoff <= len(raw_values):
        raise ValueError(f"cutoff must be an integer from {MIN_CUTOFF} through the number of observed days")
    try:
        start = date.fromisoformat(start_date)
    except (TypeError, ValueError) as exc:
        raise ValueError("start_date must be an ISO date (YYYY-MM-DD)") from exc
    # Intentionally never inspect, validate or use the future held-out values.
    values = [_number(value, "Observed sales") for value in raw_values[:cutoff]]
    if model == "xgboost":
        _modules()  # A missing optional dependency must never masquerade as a fitted model.
    calibration_origins = [cutoff - 140, cutoff - 112]
    test_origins = [cutoff - 84, cutoff - 56, cutoff - 28]
    residuals = []
    for origin in calibration_origins:
        state = _fit(values[:origin], start, model)
        predictions = _predict(state, values[max(0, origin - 28):origin], start + timedelta(days=origin))
        residuals.extend(actual - predicted for actual, predicted in zip(values[origin:origin + HORIZON], predictions))
    calibration = {"lower_offset": _quantile(residuals, 0.10), "upper_offset": _quantile(residuals, 0.90),
                   "observations": len(residuals)}
    backtests, all_actuals, all_predictions, all_baselines, all_bands = [], [], [], [], []
    for origin in test_origins:
        training = values[:origin]
        predictions = _predict(_fit(training, start, model), training[-28:], start + timedelta(days=origin))
        baseline = _predict(_fit(training, start, "seasonal"), training[-28:], start + timedelta(days=origin))
        bands = _bands(predictions, calibration)
        actuals = values[origin:origin + HORIZON]
        metrics = _metrics(actuals, predictions, baseline, bands)
        backtests.append({"cutoff_date": (start + timedelta(days=origin - 1)).isoformat(),
                          **metrics, "training_days": origin, "test_days": HORIZON})
        all_actuals.extend(actuals)
        all_predictions.extend(predictions)
        all_baselines.extend(baseline)
        all_bands.extend(bands)
    final_state = _fit(values, start, model)
    analysis = {
        "model": model,
        "history": [{"date": (start + timedelta(days=t)).isoformat(), "actual": values[t]}
                    for t in range(max(0, cutoff - 84), cutoff)],
        "metrics": _metrics(all_actuals, all_predictions, all_baselines, all_bands), "backtests": backtests,
        "training": {"observed_days": cutoff, "trained_through": (start + timedelta(days=cutoff - 1)).isoformat(),
                     "horizon": HORIZON, "model_family": "Seasonal weekday mean" if model == "seasonal" else "XGBoost Poisson regression",
                     "features": ["Last four observed same-weekday sales"] if model == "seasonal" else FEATURES,
                     "inference": "Weekly pattern repeated" if model == "seasonal" else "Recursive 28-step forecast; future lags use prior predictions",
                     "calibration_windows": [_window(start, origin) for origin in calibration_origins],
                     "backtest_windows": [_window(start, origin) for origin in test_origins],
                     "uncertainty_method": "Approximate 10th/90th percentile signed-residual bands, pooled over two earlier 28-day calibration windows; coverage is measured on three later disjoint windows.",
                     "uncertainty_limitations": "Empirical bands are not guaranteed conditional quantiles; residuals may shift over time. The central estimate is expected units, labelled p50 for display.",
                     "evaluation_note": "Three rolling chronological 28-day folds; pooled per-series WAPE and MAE, not official M5 WRMSSE. WAPE and bias are undefined (null) at zero total demand.",
                     "zero_demand_guard": final_state.get("reason"), "uses_future_labels": False,
                     "uses_prices_or_future_events": False},
    }
    return {"artifact_version": 1, "model": model, "cutoff": cutoff, "start_date": start_date,
            "tail": values[-28:], "state": final_state, "baseline_state": _fit(values, start, "seasonal"),
            "calibration": calibration, "analysis": analysis}


def predict_artifact(artifact) -> dict:
    """Run final-horizon inference from a trusted JSON model artifact."""
    if artifact.get("artifact_version") != 1:
        raise ValueError("Unsupported forecasting artifact version")
    origin = date.fromisoformat(artifact["start_date"]) + timedelta(days=artifact["cutoff"])
    predictions = _predict(artifact["state"], artifact["tail"], origin)
    baseline = _predict(artifact["baseline_state"], artifact["tail"], origin)
    bands = _bands(predictions, artifact["calibration"])
    forecast = [{"date": (origin + timedelta(days=i)).isoformat(), "p10": round(low, 4),
                 "p50": round(middle, 4), "p90": round(high, 4), "baseline": round(baseline[i], 4)}
                for i, (low, middle, high) in enumerate(bands)]
    return {**artifact["analysis"], "forecast": forecast}


def analyze_series(series, start_date, cutoff, model="seasonal") -> dict:
    return predict_artifact(train_artifact(series, start_date, cutoff, model))
