import json
import unittest

from retail_forecast.inventory import compare_policies


def flat_forecast(middle=10, upper=None):
    return [{"p10": middle, "p50": middle, "p90": middle if upper is None else upper, "baseline": middle}
            for _ in range(28)]


class InventoryTests(unittest.TestCase):
    def test_zero_lead_time_receives_before_same_day_demand(self):
        result = compare_policies([10] * 28, flat_forecast(), {"initial_stock": 0, "lead_time": 0, "review_period": 1})
        policy = result["policies"][1]
        self.assertEqual(policy["daily"][0]["received"], 10)
        self.assertEqual(policy["daily"][0]["lost_sales"], 0)
        self.assertEqual(policy["fill_rate"], 1)
        self.assertEqual(policy["units_ordered"], 280)

    def test_on_order_stock_prevents_duplicate_orders(self):
        result = compare_policies([0] * 28, flat_forecast(), {"initial_stock": 0, "lead_time": 14, "review_period": 7, "safety_days": 0})
        policy = result["policies"][1]
        self.assertEqual(policy["daily"][0]["ordered"], 210)
        self.assertEqual(policy["daily"][7]["ordered"], 0)
        self.assertEqual(policy["daily"][13]["received"], 0)
        self.assertEqual(policy["daily"][14]["received"], 210)
        self.assertEqual(policy["units_ordered"], 210)

    def test_only_future_demand_change_cannot_change_earlier_orders(self):
        first = compare_policies([10] * 28, flat_forecast(), {"initial_stock": 100})
        second = compare_policies([10] * 14 + [1000] * 14, flat_forecast(), {"initial_stock": 100})
        for a, b in zip(first["policies"], second["policies"]):
            self.assertEqual(a["daily"][:14], b["daily"][:14])
            self.assertEqual(a["daily"][14]["ordered"], b["daily"][14]["ordered"])

    def test_stock_conservation_and_cost_accounting(self):
        assumptions = {"initial_stock": 17, "lead_time": 3, "review_period": 4, "unit_cost": 6,
                       "holding_cost": 0.2, "stockout_cost": 3, "order_cost": 7}
        result = compare_policies([5, 18, 0, 10] * 7, flat_forecast(), assumptions)
        for policy in result["policies"]:
            supplied = 17 + sum(day["received"] for day in policy["daily"])
            sold = sum(day["demand"] - day["lost_sales"] for day in policy["daily"])
            self.assertEqual(supplied - sold, policy["ending_inventory"])
            self.assertEqual(policy["units_ordered"] - sum(day["received"] for day in policy["daily"]), policy["outstanding_units"])
            self.assertAlmostEqual(policy["holding_cost"], sum(day["on_hand"] for day in policy["daily"]) * 0.2)
            self.assertEqual(policy["stockout_cost"], policy["lost_units"] * 3)
            self.assertEqual(policy["ordering_cost"], sum(day["ordered"] > 0 for day in policy["daily"]) * 7)
            self.assertAlmostEqual(policy["total_cost"], policy["holding_cost"] + policy["stockout_cost"] + policy["ordering_cost"])
            self.assertEqual(policy["procurement_cost"], policy["units_ordered"] * 6)

    def test_long_lead_time_leaves_orders_in_pipeline(self):
        result = compare_policies([10] * 28, flat_forecast(), {"initial_stock": 0, "lead_time": 90})
        for policy in result["policies"]:
            self.assertEqual(sum(day["received"] for day in policy["daily"]), 0)
            self.assertEqual(policy["fill_rate"], 0)
            self.assertEqual(policy["outstanding_units"], policy["units_ordered"])

    def test_zero_demand_zero_cost_and_json_validity(self):
        result = compare_policies([0] * 28, flat_forecast(0), {"initial_stock": 0})
        self.assertTrue(all(policy["fill_rate"] == 1 for policy in result["policies"]))
        self.assertTrue(all(policy["total_cost"] == 0 for policy in result["policies"]))
        self.assertIsNone(result["savings"]["percent"])
        json.dumps(result, allow_nan=False)

    def test_invalid_assumptions_and_forecasts_rejected(self):
        for assumptions in ({"lead_time": -1}, {"lead_time": 1.5}, {"lead_time": True},
                            {"review_period": 0}, {"initial_stock": -1}, {"unit_cost": float("inf")},
                            {"holding_cost": float("nan")}, {"initial_stock": 10 ** 1000}, {"typo": 1}):
            with self.subTest(assumptions=assumptions), self.assertRaises(ValueError):
                compare_policies([10] * 28, flat_forecast(), assumptions)
        invalid = flat_forecast()
        invalid[0]["p90"] = 0
        with self.assertRaisesRegex(ValueError, "quantiles"):
            compare_policies([10] * 28, invalid, {})
        with self.assertRaises(ValueError):
            compare_policies([10] * 27, flat_forecast(), {})


if __name__ == "__main__":
    unittest.main()
