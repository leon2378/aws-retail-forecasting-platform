# Application API

The local Python server and AWS Lambda share the request handler in `retail_forecast/api.py`. The local server loads the imported dataset or the labelled synthetic sample. The AWS API reads completed, materialized forecasts from DynamoDB; it does not train models during requests.

| Method | Route | Purpose |
|---|---|---|
| GET | `/api/health` | Server status and local/AWS mode |
| GET | `/api/catalog` | Data provenance, stores, products, model availability and replay bounds |
| GET | `/api/forecast` | Historical observations, 28 predictions, backtests and training metadata |
| POST | `/api/simulate` | Score three replenishment policies on a historical holdout |
| GET | `/api/releases` | Latest model release events |
| POST | `/api/releases/demo` | Local illustrative rejection and baseline-parity approval |

Every error response is `{"error":"message"}` with an appropriate HTTP status. Requests use JSON. POST bodies are limited to 16 KB.

## Selection and replay

Forecast query parameters:

```text
/api/forecast?store=CA_1&item=FOODS_1_001&cutoff=728&model=xgboost
```

`cutoff` counts observed days, starting with day 1 on the catalog's `start_date`. It must be an integer of at least 196. The forecast starts on the following day and contains exactly 28 records. The application constrains replay to dates with a complete historical holdout; the API can also produce a final future forecast locally when no scoring labels exist, but simulation then returns an explanatory error.

Models are `seasonal` or `xgboost`. XGBoost is available locally only when the ML dependency is installed; the API never substitutes a baseline while reporting it as XGBoost.

Key forecast fields:

- `source`, `source_label`, `as_of`, `cutoff`, `store_id`, `item_id`, `model`
- `history`: last 84 observed days, each with `date` and `actual`
- `forecast`: 28 records with `date`, `p10`, `p50`, `p90`, `baseline`
- `metrics`: pooled `wape`, `mae`, `bias`, `coverage`, `baseline_wape`
- `backtests`: three chronological scored folds
- `training`: split dates, features, interval method and limitations
- `summary`: total, average, peak and change relative to the previous 28 days
- `replay`: minimum/maximum cutoff and next-day availability

WAPE, bias, coverage and change fields are fractions, not percentages. WAPE and bias are `null` when demand totals zero; change is `null` when the prior mean is zero. The central `p50` field carries expected units, not an asserted median. The empirical interval is approximate.

## Simulation

```json
{
  "store_id": "CA_1",
  "item_id": "FOODS_1_001",
  "cutoff": 728,
  "model": "seasonal",
  "assumptions": {
    "initial_stock": 120,
    "lead_time": 7,
    "review_period": 7,
    "safety_days": 3,
    "unit_cost": 4,
    "holding_cost": 0.02,
    "stockout_cost": 2,
    "order_cost": 5
  }
}
```

Missing assumptions use the defaults above; unknown fields, nonfinite numbers and invalid bounds are rejected. Initial stock, lead time, review period and safety days must be whole numbers. UI/API lead time and safety days are limited to 28 days, and review period to 1–28 days.

The result includes `policies` (`fixed`, `forecast`, `buffered`), daily stock flows, operating-cost components, procurement spend, fill rate, and `savings`. The savings comparison selects the lower-cost forecast policy after scoring against fixed ordering. Negative savings mean greater simulated cost; a zero baseline cost produces a `null` percentage. The response includes `cost_basis` and `limitations` for display.

## AWS publication contract

The DynamoDB table has `pk` and `sk` string keys:

| Partition | Sort key | Contents |
|---|---|---|
| `CATALOG` | `META` | Current completed catalog in `payload` |
| `SERIES#store#item` | `FORECAST#000000728#model` | Forecast payload and internal scoring labels |
| `COMPLETED` | `000000728` | Completed batch `run_id` and available models |
| `REPLAY` | `STATE` | Committed cursor and pending execution lock |
| `RELEASES` | timestamp/run identifier | Release audit payload |

The API requires a completed marker, an available model and a forecast row from that same run. The publisher commits the catalog, marker, release and cursor in one transaction after all rows have been written and checked. An incomplete batch is unavailable to clients. Leading-underscore fields, including held-out actuals and internal run identifiers, are removed from public forecast responses.

The cloud release workflow is controlled by SageMaker; the public API rejects the local release-demo route. The initial public demo API is suitable for non-sensitive data. Add authentication before serving private operational datasets.
