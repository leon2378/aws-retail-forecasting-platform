import contextlib
import csv
from datetime import date, timedelta
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from retail_forecast.cli import main
from retail_forecast.download import DownloadError


class DownloadCommandTests(unittest.TestCase):
    def test_download_then_import_validates_real_csv_structure(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary) / "m5"
            output = Path(temporary) / "dataset.json"

            def download(target, force):
                self.assertEqual(target, folder)
                self.assertFalse(force)
                target.mkdir()
                with (target / "calendar.csv").open("w", newline="", encoding="utf-8") as stream:
                    writer = csv.writer(stream)
                    writer.writerow(["d", "date", "wm_yr_wk"])
                    for i in range(224):
                        writer.writerow([f"d_{i + 1}", (date(2011, 1, 29) + timedelta(days=i)).isoformat(), 11101])
                with (target / "sales_train_evaluation.csv").open("w", newline="", encoding="utf-8") as stream:
                    writer = csv.writer(stream)
                    writer.writerow(["item_id", "dept_id", "cat_id", "store_id"] + [f"d_{i + 1}" for i in range(224)])
                    writer.writerow(["FOODS_1_001", "FOODS_1", "FOODS", "CA_1"] + [2] * 224)
                (target / "sell_prices.csv").write_text(
                    "store_id,item_id,wm_yr_wk,sell_price\nCA_1,FOODS_1_001,11101,3.5\n", encoding="utf-8")

            with patch("retail_forecast.download.download_m5", side_effect=download) as mocked, contextlib.redirect_stdout(io.StringIO()):
                main(["download-m5", "--folder", str(folder), "--import", "--stores", "CA_1",
                      "--max-items", "1", "--output", str(output)])
            mocked.assert_called_once()
            result = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(result["source"], "m5")
            self.assertEqual(result["total_days"], 224)
            self.assertEqual(len(result["series"]), 1)
            self.assertEqual(result["series"][0]["store_id"], "CA_1")
            self.assertEqual(result["series"][0]["values"], [2] * 224)

    def test_download_failure_preserves_existing_import_and_reports_guidance(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "dataset.json"
            output.write_text('{"source":"existing"}', encoding="utf-8")
            errors = io.StringIO()
            with patch("retail_forecast.download.download_m5", side_effect=DownloadError("Run kaggle auth login")), contextlib.redirect_stderr(errors):
                with self.assertRaises(SystemExit) as stopped:
                    main(["download-m5", "--import", "--output", str(output)])
            self.assertEqual(stopped.exception.code, 2)
            self.assertIn("Run kaggle auth login", errors.getvalue())
            self.assertEqual(json.loads(output.read_text(encoding="utf-8")), {"source": "existing"})
