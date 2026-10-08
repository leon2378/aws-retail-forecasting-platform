import csv
from datetime import date, timedelta
from pathlib import Path
import tempfile
import unittest

from retail_forecast.data import demo_dataset, import_m5


class DataTests(unittest.TestCase):
    def make_fixture(self, root):
        days = 224
        with (root / "calendar.csv").open("w", newline="", encoding="utf8") as stream:
            writer = csv.writer(stream)
            writer.writerow(["d", "date", "wm_yr_wk"])
            for i in range(days):
                writer.writerow([f"d_{i + 1}", (date(2011, 1, 29) + timedelta(days=i)).isoformat(), 11101 + i // 7])
        with (root / "sales_train_validation.csv").open("w", newline="", encoding="utf8") as stream:
            writer = csv.writer(stream)
            writer.writerow(["item_id", "dept_id", "cat_id", "store_id"] + [f"d_{i + 1}" for i in range(days)])
            for store in ("CA_1", "TX_1"):
                for item in ("FOODS_1_001", "FOODS_1_002"):
                    writer.writerow([item, "FOODS_1", "FOODS", store] + [i % 7 for i in range(days)])
        with (root / "sell_prices.csv").open("w", newline="", encoding="utf8") as stream:
            writer = csv.writer(stream)
            writer.writerow(["store_id", "item_id", "wm_yr_wk", "sell_price"])
            for store in ("CA_1", "TX_1"):
                for item in ("FOODS_1_001", "FOODS_1_002"):
                    writer.writerow([store, item, 11101, 2])
                    writer.writerow([store, item, 11102, 4])

    def test_synthetic_is_deterministic_labelled_and_independent(self):
        first, second = demo_dataset(), demo_dataset()
        self.assertEqual(first, second)
        self.assertEqual(first["source"], "synthetic")
        self.assertIn("not M5", first["source_label"])
        self.assertEqual(len({s["store_id"] for s in first["series"]}), 3)
        self.assertTrue(all(len(s["values"]) == first["total_days"] for s in first["series"]))
        first["series"][0]["values"][0] = -1
        self.assertGreaterEqual(second["series"][0]["values"][0], 0)

    def test_import_limits_per_store_and_retains_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_fixture(root)
            result = import_m5(root, max_items=1)
            self.assertEqual(len(result["series"]), 2)
            self.assertEqual(result["source"], "m5")
            self.assertEqual(result["start_date"], "2011-01-29")
            self.assertEqual(result["series"][0]["price"], 3)
            self.assertEqual(len(result["metadata"]["sha256"]), 3)
            self.assertTrue(all(len(value) == 64 for value in result["metadata"]["sha256"].values()))
            selected = import_m5(root, stores=["TX_1"], max_items=2)
            self.assertEqual(len(selected["series"]), 2)
            self.assertTrue(all(item["store_id"] == "TX_1" for item in selected["series"]))
            with self.assertRaisesRegex(ValueError, "Stores not found"):
                import_m5(root, stores=["MISSING"])

    def test_calendar_gap_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_fixture(root)
            calendar = root / "calendar.csv"
            calendar.write_text(calendar.read_text().replace("2011-01-30", "2011-01-31"))
            with self.assertRaisesRegex(ValueError, "consecutive"):
                import_m5(root)

    def test_negative_sales_and_nonfinite_prices_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_fixture(root)
            sales = root / "sales_train_validation.csv"
            sales.write_text(sales.read_text().replace("CA_1,0,1", "CA_1,-1,1", 1))
            with self.assertRaisesRegex(ValueError, "nonnegative integers"):
                import_m5(root)
            self.make_fixture(root)
            prices = root / "sell_prices.csv"
            prices.write_text(prices.read_text().replace("11101,2", "11101,nan", 1))
            with self.assertRaisesRegex(ValueError, "finite and positive"):
                import_m5(root)

    def test_missing_files_and_bad_limits_explain_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "must contain"):
                import_m5(directory)
            for invalid in (0, -1, True, 1.5):
                with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                    import_m5(directory, max_items=invalid)


if __name__ == "__main__":
    unittest.main()
