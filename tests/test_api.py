import copy
import json
import tempfile
import unittest

from retail_forecast.api import DEFAULT_ASSUMPTIONS, handle_request
from retail_forecast.storage import DynamoRepository, LocalRepository


class FakeRepository:
    mode = "local"

    def catalog(self):
        return {"default_cutoff": 300, "total_days": 328,
                "products": [{"store_id": "CA_1", "item_id": "FOODS_1_001"}],
                "models": [{"id": "seasonal", "available": True}]}

    def forecast(self, store, item, cutoff, model):
        return {"store_id": store, "item_id": item, "cutoff": cutoff, "model": model,
                "source": "synthetic", "source_label": "Synthetic test fixture", "as_of": "2016-01-01",
                "forecast": [{"p10": 7, "p50": 10, "p90": 14, "baseline": 10} for _ in range(28)],
                "_actuals": [10] * 28, "_private": "not-for-clients"}


class APITest(unittest.TestCase):
    def setUp(self):
        self.repo = FakeRepository()
        self.selection = {"store": "CA_1", "item": "FOODS_1_001", "cutoff": "300", "model": "seasonal"}

    def test_forecast_never_returns_future_observations(self):
        status, result = handle_request("GET", "/api/forecast", self.selection, None, self.repo)
        self.assertEqual(status, 200)
        self.assertFalse(any(k.startswith("_") for k in result))

    def test_cutoff_rejects_fractions_infinity_and_booleans(self):
        for cutoff in ["2.5", "NaN", "inf", True, -1, 329]:
            with self.subTest(cutoff=cutoff):
                status, _ = handle_request("GET", "/api/forecast", {**self.selection, "cutoff": cutoff}, None, self.repo)
                self.assertEqual(status, 400)

    def test_missing_product_and_unavailable_model(self):
        status, _ = handle_request("GET", "/api/forecast", {**self.selection, "item": "missing"}, None, self.repo)
        self.assertEqual(status, 404)
        status, _ = handle_request("GET", "/api/forecast", {**self.selection, "model": "xgboost"}, None, self.repo)
        self.assertEqual(status, 400)

    def test_inventory_invalid_assumptions(self):
        for assumptions in [{"lead_time": -1}, {"holding_cost": float("nan")}, {"initial_stock": True},
                            {"review_period": 0}, {"review_period": 1.5}, {"initial_stock": 10 ** 1000}, {"typo": 4}, []]:
            status, result = handle_request("POST", "/api/simulate", {},
                                            {**self.selection, "assumptions": assumptions}, self.repo)
            self.assertEqual(status, 400, result)

    def test_inventory_response_is_json_safe(self):
        status, result = handle_request("POST", "/api/simulate", {},
                                        {**self.selection, "assumptions": DEFAULT_ASSUMPTIONS}, self.repo)
        self.assertEqual(status, 200, result)
        self.assertEqual(len(result["policies"]), 3)
        json.dumps(result, allow_nan=False)

    def test_short_holdout_rejected(self):
        original = self.repo.forecast
        self.repo.forecast = lambda *args: {**original(*args), "_actuals": [1] * 27}
        status, _ = handle_request("POST", "/api/simulate", {}, self.selection, self.repo)
        self.assertEqual(status, 400)

    def test_cloud_release_demo_is_forbidden(self):
        repo = DynamoRepository(table=object())
        status, _ = handle_request("POST", "/api/releases/demo", {}, {}, repo)
        self.assertEqual(status, 403)


class FakeTable:
    def __init__(self, complete):
        self.complete = complete

    def get_item(self, Key, **kwargs):
        if Key["pk"] == "COMPLETED":
            return {"Item": {"models": ["seasonal"], "run_id": "completed-run"}} if self.complete else {}
        if Key["pk"] == "CATALOG":
            return {"Item": {"payload": {"default_cutoff": 300, "min_cutoff": 300}}}
        return {"Item": {"payload": {"cutoff": 300, "_run_id": "completed-run"}}}


class PublicationTests(unittest.TestCase):
    def test_partial_batch_cannot_be_read(self):
        repo = DynamoRepository(table=FakeTable(False))
        with self.assertRaises(LookupError):
            repo.forecast("CA_1", "FOODS_1_001", 300, "seasonal")

    def test_only_committed_model_can_be_read(self):
        repo = DynamoRepository(table=FakeTable(True))
        forecast = repo.forecast("CA_1", "FOODS_1_001", 300, "seasonal")
        self.assertEqual(forecast["cutoff"], 300)
        self.assertFalse(forecast["replay"]["can_advance"])
        with self.assertRaises(LookupError):
            repo.forecast("CA_1", "FOODS_1_001", 300, "xgboost")

    def test_row_from_another_run_cannot_be_read(self):
        table = FakeTable(True)
        original_get = table.get_item
        def get_item(Key, **kwargs):
            result = original_get(Key, **kwargs)
            if Key["pk"].startswith("SERIES#"):
                result["Item"]["payload"]["_run_id"] = "uncommitted-run"
            return result
        table.get_item = get_item
        with self.assertRaises(LookupError):
            DynamoRepository(table=table).forecast("CA_1", "FOODS_1_001", 300, "seasonal")


class LocalIntegrationTests(unittest.TestCase):
    def test_demo_releases_persist_and_do_not_promote_forecast(self):
        with tempfile.TemporaryDirectory() as folder:
            repo = LocalRepository(folder)
            catalog_before = copy.deepcopy(repo.catalog())
            releases = repo.demo_releases()["releases"]
            self.assertEqual([r["status"] for r in releases], ["Rejected", "Approved"])
            self.assertTrue(all(r["workflow_demo"] for r in releases))
            self.assertEqual(repo.catalog(), catalog_before)
            self.assertEqual(LocalRepository(folder).releases()["releases"], releases)


if __name__ == "__main__":
    unittest.main()
