from datetime import date, timedelta
import importlib.util
import json
import unittest
from unittest.mock import patch

from retail_forecast.forecast import analyze_series, predict_artifact, train_artifact


class ForecastTests(unittest.TestCase):
    def setUp(self):
        self.series = {"values": [4, 6, 8, 10, 12, 14, 16] * 40}

    def test_exact_weekly_pattern_and_date_alignment(self):
        result = analyze_series(self.series, "2020-01-01", 196)
        self.assertEqual(len(result["forecast"]), 28)
        self.assertEqual(len(result["history"]), 84)
        self.assertEqual(result["forecast"][0]["date"], "2020-07-15")
        self.assertEqual(result["history"][-1]["date"], "2020-07-14")
        self.assertEqual([row["p50"] for row in result["forecast"]], self.series["values"][196:224])
        self.assertEqual(result["metrics"]["wape"], 0)
        self.assertEqual(result["metrics"]["coverage"], 1)

    def test_future_labels_have_no_effect_even_if_invalid(self):
        first = analyze_series(self.series, "2020-01-01", 196)
        changed = {"values": self.series["values"][:196] + [float("nan"), -1, "future-secret"]}
        self.assertEqual(first, analyze_series(changed, "2020-01-01", 196))

    def test_calibration_precedes_disjoint_tests_and_excludes_final_test(self):
        artifact = train_artifact(self.series, "2020-01-01", 252)
        training = artifact["analysis"]["training"]
        windows = training["calibration_windows"] + training["backtest_windows"]
        for previous, following in zip(windows, windows[1:]):
            self.assertEqual(date.fromisoformat(previous["test_end_date"]) + timedelta(days=1),
                             date.fromisoformat(following["test_start_date"]))
        changed = {"values": list(self.series["values"])}
        changed["values"][224:252] = [1000] * 28
        next_artifact = train_artifact(changed, "2020-01-01", 252)
        self.assertEqual(artifact["calibration"], next_artifact["calibration"])
        self.assertEqual(artifact["analysis"]["backtests"][:2], next_artifact["analysis"]["backtests"][:2])
        self.assertNotEqual(artifact["analysis"]["metrics"], next_artifact["analysis"]["metrics"])

    def test_model_artifact_roundtrip_separates_inference(self):
        artifact = train_artifact(self.series, "2020-01-01", 196)
        self.assertNotIn("forecast", artifact["analysis"])
        restored = json.loads(json.dumps(artifact, allow_nan=False))
        self.assertEqual(predict_artifact(restored), analyze_series(self.series, "2020-01-01", 196))

    def test_zero_demand_is_finite_and_wape_undefined(self):
        result = analyze_series({"values": [0] * 224}, "2020-01-01", 196)
        self.assertIsNone(result["metrics"]["wape"])
        self.assertIsNone(result["metrics"]["bias"])
        self.assertEqual(result["metrics"]["mae"], 0)
        self.assertEqual(result["metrics"]["coverage"], 1)
        json.dumps(result, allow_nan=False)

    def test_invalid_observations_cutoffs_and_models_rejected(self):
        for cutoff in (195, 999, True, 196.0):
            with self.subTest(cutoff=cutoff), self.assertRaises(ValueError):
                analyze_series(self.series, "2020-01-01", cutoff)
        for bad in (-1, float("nan"), float("inf"), True, "2", 10 ** 1000, 1e300):
            values = [0] * 196
            values[0] = bad
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                analyze_series({"values": values}, "2020-01-01", 196)
        with self.assertRaises(ValueError):
            analyze_series(self.series, "2020-01-01", 196, "made-up")

    def test_missing_xgboost_never_silently_falls_back(self):
        with patch("retail_forecast.forecast._modules", side_effect=ValueError("XGBoost is not installed")):
            with self.assertRaisesRegex(ValueError, "not installed"):
                analyze_series(self.series, "2020-01-01", 196, "xgboost")

    @unittest.skipUnless(importlib.util.find_spec("xgboost") and importlib.util.find_spec("numpy"),
                         "optional XGBoost dependencies are not installed")
    def test_real_xgboost_artifact_and_future_independence(self):
        artifact = train_artifact(self.series, "2020-01-01", 196, "xgboost")
        self.assertEqual(artifact["state"]["type"], "xgboost")
        self.assertIn("booster", artifact["state"])
        result = predict_artifact(json.loads(json.dumps(artifact, allow_nan=False)))
        changed = {"values": self.series["values"][:196] + [999999] * 84}
        self.assertEqual(result, analyze_series(changed, "2020-01-01", 196, "xgboost"))
        self.assertEqual(len(result["forecast"]), 28)
        self.assertTrue(all(0 <= row["p10"] <= row["p50"] <= row["p90"] for row in result["forecast"]))


if __name__ == "__main__":
    unittest.main()
