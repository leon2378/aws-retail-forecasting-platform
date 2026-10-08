"""Data corruption gates and observed-history review without raw-data leakage."""
import copy
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from retail_forecast.quality import (MAX_OBSERVATIONS, MAX_SNAPSHOT_BYTES, inspect_snapshot, parse_snapshot,
                                    read_snapshot, require_snapshot, SnapshotQualityError)


def snapshot():
    return {"source": "synthetic", "source_label": "Generated test fixture", "start_date": "2020-01-01",
            "total_days": 252, "metadata": {}, "series": [
                {"store_id": "CA_1", "item_id": "ITEM_1", "name": "Generated item", "category": "FOODS",
                 "department": "FOODS_1", "price": 4, "values": [10] * 252},
                {"store_id": "TX_1", "item_id": "ITEM_2", "name": "Another generated item", "category": "FOODS",
                 "department": "FOODS_1", "price": 5, "values": [10] * 252}]}


class QualityTests(unittest.TestCase):
    def test_valid_snapshot_report_is_bounded_strict_json_and_does_not_expose_sales(self):
        report = require_snapshot(snapshot(), cutoff=196)
        self.assertTrue(report["passed"])
        self.assertEqual(report["cutoff"], 196)
        self.assertEqual(report["series_count"], 2)
        self.assertEqual(len(report["snapshot_sha256"]), 64)
        encoded = json.dumps(report, allow_nan=False)
        self.assertNotIn('"values"', encoded)
        self.assertLess(len(encoded), 5000)
        self.assertLessEqual(len(report["checks"]), 10)
        self.assertEqual(report["issues"], [])

    def test_missing_unexpected_or_duplicate_identity_blocks_acceptance(self):
        expected = snapshot()
        for change in (lambda data: data["series"].pop(),
                       lambda data: data["series"].append(copy.deepcopy(data["series"][0])),
                       lambda data: data["series"][0].update(item_id="ITEM_99")):
            data = copy.deepcopy(expected)
            change(data)
            with self.subTest(size=len(data["series"])):
                self.assertFalse(inspect_snapshot(data, cutoff=196, expected=expected)["passed"])
        products = [{"store_id": item["store_id"], "item_id": item["item_id"]} for item in expected["series"]]
        self.assertTrue(require_snapshot(expected, expected=products)["passed"])
        self.assertTrue(require_snapshot(expected, expected={"products": products, "start_date": "2020-01-01"})["passed"])
        # DynamoDB returns catalog numbers as Decimal; growth checks must still
        # accept the same catalog when it is loaded through the real API.
        dynamo_catalog = {"products": products, "source": "synthetic", "start_date": "2020-01-01",
                          "total_days": Decimal("252")}
        self.assertTrue(require_snapshot(expected, expected=dynamo_catalog)["passed"])

    def test_calendar_or_source_change_against_accepted_snapshot_blocks(self):
        expected = snapshot()
        for field, value in (("source", "m5"), ("start_date", "2020-02-01"), ("total_days", 251)):
            changed = copy.deepcopy(expected)
            changed[field] = value
            if field == "total_days":
                for series in changed["series"]:
                    series["values"] = series["values"][:251]
            self.assertFalse(inspect_snapshot(changed, expected=expected)["passed"])
        extended = copy.deepcopy(expected)
        extended["total_days"] += 1
        for series in extended["series"]:
            series["values"].append(10)
        self.assertTrue(inspect_snapshot(extended, cutoff=196, expected=expected)["passed"])

    def test_invalid_sales_fail_without_echoing_observation(self):
        for value in (-1, 1.2, True, float("nan"), float("inf"), "private-observation", 10**1000, 100_000_001):
            changed = snapshot()
            changed["series"][0]["values"][0] = value
            report = inspect_snapshot(changed, cutoff=196)
            self.assertFalse(report["passed"])
            self.assertIn("daily_sales", [issue["code"] for issue in report["issues"]])
            self.assertNotIn("private-observation", json.dumps(report, allow_nan=False))
        changed = snapshot()
        changed["series"][0]["values"].pop()
        self.assertFalse(inspect_snapshot(changed)["passed"])

    def test_missing_display_fields_invalid_ranges_and_booleans_fail(self):
        for field, value in (("name", ""), ("category", None), ("department", ""), ("price", True),
                             ("price", 0), ("price", float("inf"))):
            changed = snapshot()
            changed["series"][0][field] = value
            self.assertFalse(inspect_snapshot(changed)["passed"])
        for cutoff in (True, 195, 225, 196.5, 196.0, Decimal("196")):
            self.assertFalse(inspect_snapshot(snapshot(), cutoff=cutoff)["passed"])
        for field, value in (("total_days", True), ("total_days", 223), ("total_days", 252.0), ("start_date", "2020-13-01"),
                             ("source", "unverified"), ("source", []), ("metadata", []), ("series", [])):
            changed = snapshot()
            changed[field] = value
            self.assertFalse(inspect_snapshot(changed)["passed"])

    def test_large_observed_shift_warns_without_blocking_and_future_labels_have_no_influence(self):
        changed = snapshot()
        changed["series"][0]["values"][168:196] = [60] * 28
        report = inspect_snapshot(changed, cutoff=196)
        self.assertTrue(report["passed"])
        warnings = [issue for issue in report["issues"] if issue["severity"] == "warning"]
        self.assertEqual([issue["code"] for issue in warnings], ["observed_sales_shift"])
        changed["series"][0]["values"][196:] = [99_999_999] * 56
        future_report = inspect_snapshot(changed, cutoff=196)
        self.assertEqual(report["issues"], future_report["issues"])
        self.assertEqual(report["checks"], future_report["checks"])
        clean = snapshot()
        clean["series"][0]["values"][196:] = [99_999_999] * 56
        self.assertEqual(inspect_snapshot(clean, cutoff=196)["issues"], [])

    def test_invalid_json_has_bounded_report_and_readable_exception(self):
        for raw in (b"invalid private observations", b'\xff', '{"series":NaN}', '{"series":[],"series":[]}', None):
            value, report = parse_snapshot(raw, cutoff=196)
            self.assertIsNone(value)
            self.assertFalse(report["passed"])
            self.assertEqual(report["issues"][0]["code"], "snapshot_parse")
            self.assertNotIn("private observations", json.dumps(report, allow_nan=False))
        value, report = parse_snapshot(json.dumps(snapshot()), cutoff=196)
        self.assertEqual(value, snapshot())
        self.assertTrue(report["passed"])
        changed = snapshot()
        changed["series"][0]["values"][0] = -1
        with self.assertRaises(SnapshotQualityError) as caught:
            require_snapshot(changed)
        self.assertFalse(caught.exception.report["passed"])
        self.assertIn("daily_sales", str(caught.exception))

    def test_scoped_observation_budget_rejects_before_hashing_or_traversing_sales(self):
        full_m5 = snapshot()
        full_m5["total_days"] = 1941
        full_m5["series"] = [full_m5["series"][0]] * 30_490
        with patch("retail_forecast.quality.json.JSONEncoder", side_effect=AssertionError("Oversized source must not be normalized")):
            report = inspect_snapshot(full_m5)
        self.assertFalse(report["passed"])
        self.assertEqual(report["issues"][0]["code"], "snapshot_budget")
        self.assertIsNone(report["snapshot_sha256"])
        actual_oversized = snapshot()
        actual_oversized["series"] = [actual_oversized["series"][0]]
        actual_oversized["series"][0]["values"] = [0] * (MAX_OBSERVATIONS + 1)
        with patch("retail_forecast.quality.json.JSONEncoder", side_effect=AssertionError("Do not serialize oversized arrays")):
            self.assertFalse(inspect_snapshot(actual_oversized)["passed"])

    def test_byte_budget_rejects_before_json_parse_and_before_file_body_read(self):
        oversized = b" " * (MAX_SNAPSHOT_BYTES + 1)
        with patch("retail_forecast.quality.json.loads", side_effect=AssertionError("Do not parse oversized JSON")):
            value, report = parse_snapshot(oversized, cutoff=196)
        self.assertIsNone(value)
        self.assertEqual(report["issues"][0]["code"], "snapshot_budget")
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "large.json"
            with path.open("wb") as stream:
                stream.seek(MAX_SNAPSHOT_BYTES)
                stream.write(b" ")
            with patch.object(Path, "open", side_effect=AssertionError("Do not open oversized source body")):
                value, report = read_snapshot(path, cutoff=196)
            self.assertIsNone(value)
            self.assertEqual(report["issues"][0]["code"], "snapshot_budget")


if __name__ == "__main__":
    unittest.main()
