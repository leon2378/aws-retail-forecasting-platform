"""Transparent lost-sales replenishment simulation, never an observed saving."""
from __future__ import annotations

import math
from numbers import Real
import statistics

DEFAULT_ASSUMPTIONS = {"initial_stock": 120, "lead_time": 7, "review_period": 7, "safety_days": 3,
                       "unit_cost": 4, "holding_cost": 0.02, "stockout_cost": 2, "order_cost": 5}


def _finite(value, name, maximum=100_000_000):
    if isinstance(value, bool) or not isinstance(value, Real) or not 0 <= value <= maximum or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number between 0 and {maximum}")
    return float(value)


def _assumptions(raw):
    if not isinstance(raw, dict):
        raise ValueError("assumptions must be an object")
    unknown = set(raw) - set(DEFAULT_ASSUMPTIONS)
    if unknown:
        raise ValueError("Unknown assumptions: " + ", ".join(sorted(unknown)))
    values = {**DEFAULT_ASSUMPTIONS, **raw}
    for name, value in values.items():
        maximum = {"lead_time": 90, "review_period": 28, "safety_days": 60,
                   "initial_stock": 10_000_000}.get(name, 1_000_000)
        validated = _finite(value, name, maximum)
        if name in ("initial_stock", "lead_time", "review_period", "safety_days"):
            if int(validated) != validated:
                raise ValueError(f"{name} must be a whole number")
            values[name] = int(validated)
        else:
            values[name] = validated
    if values["review_period"] < 1:
        raise ValueError("review_period must be at least 1 day")
    return values


def compare_policies(actuals, forecast, assumptions) -> dict:
    """Compare fixed-quantity, mean order-up-to and buffered order-up-to policies.

    Each day's sequence is receive -> review/order -> immediate arrivals -> sales.
    Inventory position includes all pipeline orders. Unsatisfied sales are lost,
    never backlogged. Orders use forecasts only; actuals affect scoring and the
    current on-hand balance, as they would in an operational replenishment loop.
    """
    if not isinstance(actuals, (list, tuple)) or len(actuals) != 28:
        raise ValueError("Simulation requires exactly 28 observed future daily sales")
    demands = [_finite(value, "actual demand") for value in actuals]
    if any(value != int(value) for value in demands):
        raise ValueError("Actual demand must contain whole units")
    demands = [int(value) for value in demands]
    if not isinstance(forecast, (list, tuple)) or len(forecast) != 28:
        raise ValueError("Simulation requires exactly 28 forecast days")
    estimates = []
    for row in forecast:
        if not isinstance(row, dict) or any(key not in row for key in ("p10", "p50", "p90", "baseline")):
            raise ValueError("Every forecast day requires p10, p50, p90 and baseline")
        clean = {key: _finite(row[key], key) for key in ("p10", "p50", "p90", "baseline")}
        if not clean["p10"] <= clean["p50"] <= clean["p90"]:
            raise ValueError("Forecast quantiles must satisfy p10 <= p50 <= p90")
        estimates.append(clean)
    settings = _assumptions(assumptions)
    lead, review = settings["lead_time"], settings["review_period"]
    fixed_quantity = math.ceil(statistics.fmean(row["baseline"] for row in estimates) * review)
    horizon = lead + review
    policies = []
    for policy_id, name in (("fixed", "Fixed quantity"), ("forecast", "Forecast order-up-to"),
                            ("buffered", "Buffered forecast")):
        stock = settings["initial_stock"]
        pipeline = {}
        daily = []
        holding = lost_cost = ordering = 0.0
        ordered_units = lost_units = stockout_days = 0
        for day, demand in enumerate(demands):
            received = pipeline.pop(day, 0)
            stock += received
            quantity = 0
            if day % review == 0:
                position = stock + sum(pipeline.values())
                if policy_id == "fixed":
                    quantity = fixed_quantity
                else:
                    column = "p90" if policy_id == "buffered" else "p50"
                    target = sum(estimates[(day + offset) % 28][column] for offset in range(horizon))
                    if policy_id == "buffered":
                        target += sum(estimates[(day + horizon + offset) % 28]["p50"]
                                      for offset in range(settings["safety_days"]))
                    quantity = max(0, math.ceil(target - position))
                if quantity:
                    ordered_units += quantity
                    ordering += settings["order_cost"]
                    if lead == 0:
                        stock += quantity
                        received += quantity
                    else:
                        arrival = day + lead
                        pipeline[arrival] = pipeline.get(arrival, 0) + quantity
            lost = max(0, demand - stock)
            stock = max(0, stock - demand)
            lost_units += lost
            stockout_days += int(lost > 0)
            holding += stock * settings["holding_cost"]
            lost_cost += lost * settings["stockout_cost"]
            daily.append({"day": day + 1, "demand": demand, "on_hand": stock,
                          "ordered": quantity, "received": received, "lost_sales": lost,
                          "on_order": sum(pipeline.values())})
        total = holding + lost_cost + ordering
        policies.append({"id": policy_id, "name": name, "fill_rate": 1 - lost_units / sum(demands) if sum(demands) else 1.0,
                         "total_cost": round(total, 2), "holding_cost": round(holding, 2),
                         "stockout_cost": round(lost_cost, 2), "ordering_cost": round(ordering, 2),
                         "units_ordered": ordered_units, "procurement_cost": round(ordered_units * settings["unit_cost"], 2),
                         "ending_inventory": stock, "outstanding_units": sum(pipeline.values()),
                         "stockout_days": stockout_days, "lost_units": lost_units, "daily": daily})
    fixed_cost = policies[0]["total_cost"]
    winner = min(policies[1:], key=lambda policy: policy["total_cost"])
    savings = round(fixed_cost - winner["total_cost"], 2)
    return {"policies": policies, "savings": {"amount": savings, "percent": savings / fixed_cost if fixed_cost else None,
                                               "compared_with": "fixed", "policy_id": winner["id"]},
            "label": "Simulation results only · historical sales replay, not realized savings",
            "assumptions": settings,
            "cost_basis": "Total cost = end-of-day inventory holding + lost-sales penalty + fixed order fees. Procurement spend is shown separately; unit_cost does not affect total cost or savings.",
            "limitations": ["Lost sales, deterministic lead times, unlimited supplier capacity and no opening pipeline orders.",
                            "Historical observed sales are treated as demand; stock-constrained sales can understate unmet demand.",
                            "Planning beyond day 28 repeats the 28-day forecast; this is an approximation.",
                            "Orders arriving beyond the 28-day scoring window remain outstanding; terminal stock has no salvage value.",
                            "The lowest simulated cost is selected after scoring and is not a prospective guarantee."]}
