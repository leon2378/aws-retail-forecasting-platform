"""Deterministic demo data and a bounded, streaming M5 CSV importer.

M5 sales are observed sales, not uncensored demand. No dataset is bundled or
downloaded automatically; users obtain the competition files under its terms.
"""
from __future__ import annotations

import csv
from datetime import date, timedelta
import hashlib
import math
from pathlib import Path
import random


def demo_dataset() -> dict:
    """Return explicitly synthetic data; identifiers mimic M5's hierarchy only."""
    rng = random.Random(20261007)
    start = date(2014, 1, 1)
    days = 756
    products = [
        ("FOODS_1_001", "FOODS", "FOODS_1", "Breakfast oats", 15, 4.20),
        ("FOODS_2_001", "FOODS", "FOODS_2", "Wholegrain bread", 24, 3.10),
        ("FOODS_3_001", "FOODS", "FOODS_3", "Sparkling water", 20, 2.40),
        ("HOUSEHOLD_1_001", "HOUSEHOLD", "HOUSEHOLD_1", "Laundry detergent", 7, 8.50),
        ("HOUSEHOLD_2_001", "HOUSEHOLD", "HOUSEHOLD_2", "Kitchen towels", 11, 5.20),
        ("HOBBIES_1_001", "HOBBIES", "HOBBIES_1", "Craft notebook", 4, 3.80),
    ]
    series = []
    for store_index, store in enumerate(("CA_1", "TX_1", "WI_1")):
        for product_index, (item, category, department, name, level, price) in enumerate(products):
            values = []
            for t in range(days):
                day = start + timedelta(days=t)
                weekday = (0.88, 0.90, 0.95, 1.0, 1.15, 1.28, 1.06)[day.weekday()]
                seasonal = 1 + 0.17 * math.sin(2 * math.pi * (t + product_index * 24) / 365.25)
                growth = 1 + 0.00022 * t
                mean = level * (1 + store_index * 0.13) * weekday * seasonal * growth
                # Occasional planned-looking bursts and random noise, all synthetic.
                if t % 91 in (18, 19, 20):
                    mean *= 1.25
                value = max(0, round(rng.gauss(mean, math.sqrt(mean) * 0.9)))
                if product_index == 5 and rng.random() < 0.08:
                    value = 0
                values.append(value)
            series.append({"store_id": store, "item_id": item, "category": category,
                           "department": department, "name": name, "values": values,
                           "price": price})
    return {
        "source": "synthetic", "source_label": "Synthetic demonstration · not M5 sales",
        "start_date": start.isoformat(), "total_days": days, "series": series,
        "metadata": {"generator": "deterministic-retail-v1", "seed": 20261007,
                     "currency": "USD", "prices": "Illustrative synthetic unit prices",
                     "product_names": "Invented for the demo; not M5 item descriptions",
                     "limitations": ["Synthetic observations do not establish real forecast accuracy or savings.",
                                     "Historical sales may be constrained by stock availability."]},
    }


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _reader(path: Path):
    return path.open("r", newline="", encoding="utf-8-sig")


def _require_columns(fieldnames, required, filename):
    missing = set(required) - set(fieldnames or [])
    if missing:
        raise ValueError(f"{filename} is missing columns: {', '.join(sorted(missing))}")
    if len(fieldnames or []) != len(set(fieldnames or [])):
        raise ValueError(f"{filename} has duplicate column names")


def import_m5(folder, stores=None, max_items=12) -> dict:
    """Read at most ``max_items`` items per selected store, streaming each CSV.

    Use evaluation sales if present, otherwise validation sales. Every selected
    series must contain contiguous nonnegative integer day columns. Calendar
    dates are checked for gaps. Prices are descriptive medians across available
    weeks and NEVER model inputs, because these may include weeks after replay.
    """
    if isinstance(max_items, bool) or not isinstance(max_items, int) or not 1 <= max_items <= 3049:
        raise ValueError("max_items must be an integer between 1 and 3049 per store")
    if isinstance(stores, str):
        stores = [stores]
    selected_stores = None if stores is None else set(stores)
    if selected_stores is not None and (not selected_stores or
                                       any(not isinstance(s, str) or not s for s in selected_stores)):
        raise ValueError("stores must contain at least one nonempty store identifier")
    root = Path(folder)
    sales = root / "sales_train_evaluation.csv"
    if not sales.is_file():
        sales = root / "sales_train_validation.csv"
    calendar, prices = root / "calendar.csv", root / "sell_prices.csv"
    missing = [p.name for p in (sales, calendar, prices) if not p.is_file()]
    if missing:
        raise ValueError("M5 folder must contain sales_train_evaluation.csv (or sales_train_validation.csv), "
                         "calendar.csv and sell_prices.csv; missing: " + ", ".join(missing))

    dates = {}
    with _reader(calendar) as stream:
        reader = csv.DictReader(stream)
        _require_columns(reader.fieldnames, ("d", "date", "wm_yr_wk"), calendar.name)
        for row in reader:
            day_key = row["d"]
            if not day_key.startswith("d_") or not day_key[2:].isdigit():
                raise ValueError(f"Invalid calendar day: {day_key!r}")
            if day_key in dates:
                raise ValueError(f"Duplicate calendar day: {day_key}")
            try:
                dates[day_key] = date.fromisoformat(row["date"])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Invalid date for calendar day {day_key}") from exc

    result, counts, seen, available_stores = [], {}, set(), set()
    with _reader(sales) as stream:
        reader = csv.DictReader(stream)
        _require_columns(reader.fieldnames, ("item_id", "dept_id", "cat_id", "store_id"), sales.name)
        day_columns = [c for c in reader.fieldnames if c.startswith("d_")]
        expected = [f"d_{i}" for i in range(1, len(day_columns) + 1)]
        if day_columns != expected or len(day_columns) < 224:
            raise ValueError("Sales day columns must be ordered, contiguous d_1...d_N with at least 224 days")
        if any(day not in dates for day in expected):
            raise ValueError("Calendar does not cover every sales day")
        start = dates["d_1"]
        if any(dates[key] != start + timedelta(days=i) for i, key in enumerate(expected)):
            raise ValueError("Calendar sales dates must be consecutive daily observations")
        for row_number, row in enumerate(reader, start=2):
            store = row["store_id"]
            if not store:
                raise ValueError(f"Empty store_id at sales row {row_number}")
            available_stores.add(store)
            if selected_stores is not None and store not in selected_stores:
                continue
            key = (store, row["item_id"])
            if key in seen:
                raise ValueError(f"Duplicate selected store/item: {store}/{key[1]}")
            if counts.get(store, 0) >= max_items:
                continue
            if any(not row.get(c) for c in ("item_id", "dept_id", "cat_id")):
                raise ValueError(f"Missing product identity at sales row {row_number}")
            values = []
            for column in day_columns:
                raw = row.get(column)
                if raw is None or not raw.isascii() or not raw.isdecimal():
                    raise ValueError(f"Sales must be nonnegative integers: row {row_number}, {column}")
                value = int(raw)
                if value > 100_000_000:
                    raise ValueError(f"Unreasonable sales value: row {row_number}, {column}")
                values.append(value)
            seen.add(key)
            counts[store] = counts.get(store, 0) + 1
            result.append({"store_id": store, "item_id": row["item_id"], "category": row["cat_id"],
                           "department": row["dept_id"], "name": row["item_id"], "values": values})
    if selected_stores is not None and selected_stores - available_stores:
        raise ValueError("Stores not found in sales file: " + ", ".join(sorted(selected_stores - available_stores)))
    if not result:
        raise ValueError("No sales series matched the selection")

    # Keep prices only for selected products, so full M5 price CSV is not loaded.
    selected_prices = {key: [] for key in seen}
    price_weeks = set()
    with _reader(prices) as stream:
        reader = csv.DictReader(stream)
        _require_columns(reader.fieldnames, ("store_id", "item_id", "wm_yr_wk", "sell_price"), prices.name)
        for row in reader:
            key = (row["store_id"], row["item_id"])
            if key not in selected_prices:
                continue
            week_key = (*key, row["wm_yr_wk"])
            if week_key in price_weeks:
                raise ValueError(f"Duplicate selected price week for {key[0]}/{key[1]}")
            price_weeks.add(week_key)
            try:
                value = float(row["sell_price"])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Invalid price for {key[0]}/{key[1]}") from exc
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"Price must be finite and positive for {key[0]}/{key[1]}")
            selected_prices[key].append(value)
    for item in result:
        values = sorted(selected_prices[(item["store_id"], item["item_id"])])
        if not values:
            raise ValueError(f"No prices found for {item['store_id']}/{item['item_id']}")
        midpoint = len(values) // 2
        item["price"] = round((values[midpoint] + values[(len(values) - 1) // 2]) / 2, 4)
    return {
        "source": "m5", "source_label": "M5 historical sales · replay simulation",
        "start_date": start.isoformat(), "total_days": len(day_columns), "series": result,
        "metadata": {"dataset": "M5 Forecasting - Accuracy", "sales_file": sales.name,
                     "sha256": {p.name: _hash_file(p) for p in (sales, calendar, prices)},
                     "max_items_per_store": max_items, "stores": sorted(counts), "currency": "USD",
                     "prices": "Descriptive median of supplied weekly prices; never used as forecast features",
                     "limitations": ["Observed sales are a proxy for demand and may be stock-constrained.",
                                     "Product descriptions are unavailable; item IDs are retained.",
                                     "Subset ordinary WAPE/MAE is not the official hierarchical M5 WRMSSE metric."]},
    }
