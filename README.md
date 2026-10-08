# SupplySight

A retail demand forecasting and replenishment application, with a local development mode and an AWS deployment foundation in **ap-southeast-2 (Sydney)**.

Select a store and product, replay a historical date, inspect the next 28 days of expected sales and approximate uncertainty, and compare inventory policies against held-out historical sales. The model lab compares a seasonal baseline with XGBoost using chronological backtests and demonstrates a rejected release followed by an approved one.

**Data status:** the repository includes a deterministic synthetic generator, not M5 observations. The interface labels it clearly. Genuine M5 CSVs have been downloaded and imported privately for local validation: 36 series from CA_1, TX_1 and WI_1, covering 1,941 historical days. A fresh checkout uses synthetic data until you import your own authorized M5 copy. Synthetic product names are invented; real M5 items keep their anonymous identifiers. No genuine M5 rows are distributed in this repository.

**Deployment status:** on 8 October 2026, the 43-resource AWS foundation was deployed and passed 21 live infrastructure, API and frontend smoke checks. All 43 resources were subsequently removed to stop ongoing project charges. Development currently runs locally; there is no active hosted application. The Linux image build, offline ML workflow and inference HTTP protocol have passed local validation. Live SageMaker execution, Model Registry and DynamoDB publication remain unverified. Local execution does not need AWS credentials.

![SupplySight forecast workspace using labelled synthetic data](docs/images/forecast-demo.jpg)

## Run locally

Python 3.11 or newer is required. The seasonal baseline and local application use the standard library only:

```powershell
python -m retail_forecast serve --port 8000
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000). Stop the server with Ctrl+C.

The sign-in screen provides a **local access preview**. Choose Viewer to inspect forecasts and operations, or Planner to run inventory comparisons and local demonstrations. These generated identities exercise server-enforced permissions; they do not authenticate real users. Sessions expire and are cleared when the server restarts. See [the sign-in guide](docs/authentication.md) for the Cognito deployment configuration and security boundaries.

![SupplySight local access preview with Viewer and Planner permissions](docs/images/signin-demo.png)

To enable real XGBoost training and the AWS tooling:

```powershell
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -e ".[ml,aws]"
./.venv/Scripts/python.exe -m retail_forecast serve --port 8000
```

On macOS/Linux use `.venv/bin/python` instead. The frontend has no npm build, external scripts, CDN fonts, or runtime third-party web dependency. Run from this repository; its `frontend/` directory is served alongside the API. For an installed editable package, the `shelfcast` command also works.

## Import M5

For automatic download and import, install the optional official Kaggle client and sign in once:

```powershell
./.venv/Scripts/python.exe -m pip install -e '.[data]'
./.venv/Scripts/kaggle.exe auth login
./.venv/Scripts/python.exe -m retail_forecast download-m5 --import
```

Complete the browser sign-in prompts; the client saves your authorization locally. If Kaggle asks you to join the competition or accept its rules, do that on the [M5 competition page](https://www.kaggle.com/competitions/m5-forecasting-accuracy) before downloading. The command downloads only the three required CSVs into `data/m5`, safely extracts compressed responses, validates the data and imports 12 products per store from CA_1, TX_1 and WI_1. Use `--stores` and `--max-items` to change that subset. Existing valid files are reused; `--force` downloads replacements.

The official client also supports an API token in `~/.kaggle/access_token`, or legacy credentials in `~/.kaggle/kaggle.json`. Configure credentials locally, outside this repository. Downloaded data and credential filenames are ignored by Git. See [Kaggle's authentication documentation](https://github.com/Kaggle/kaggle-cli/blob/main/docs/README.md#authentication).

Restart the local server after import to replace the synthetic demonstration with M5 sales.

For a manual download:

Obtain the data through the official [M5 Forecasting – Accuracy dataset page](https://www.kaggle.com/competitions/m5-forecasting-accuracy/data), following its competition terms. Extract these files into a local folder:

- `sales_train_evaluation.csv` (preferred), or `sales_train_validation.csv`
- `calendar.csv`
- `sell_prices.csv`

```powershell
./.venv/Scripts/python.exe -m retail_forecast import-m5 "C:/path/to/m5" --stores CA_1 TX_1 WI_1 --max-items 12
./.venv/Scripts/python.exe -m retail_forecast serve --port 8000
```

Restart an already-running server after importing. The importer writes `data/dataset.json`, validates day continuity, numeric sales, selected identities and price records, and records source SHA-256 hashes. It streams CSVs and defaults to 12 items **per store** to keep iteration practical. Selection follows file order, so this is not a representative sample of every category. Raise `--max-items` deliberately; the initial ML workflow fits each series independently and is not optimized for the entire M5 hierarchy.

Real M5 data uses item IDs, not the demo's invented descriptions. Sell prices are retained as descriptive metadata; the current forecasts do **not** use future prices or event information. Keep raw data outside source control; `data/` is ignored.

The documented local validation uses the first 12 items per selected store, all from `HOBBIES_1`, at cutoff 1,913. Across three chronological 28-day backtests, the tested native and Linux environments produced these results:

| Environment and candidate | Pooled WAPE | Average interval coverage | Local release gate |
|---|---:|---:|---|
| Both environments · seasonal baseline | 108.07% | 82.34% | Pass at parity |
| Native Windows · XGBoost 3.4.1 | 109.02% | 76.69% | Reject |
| Linux image · XGBoost 3.2.0 | 110.21% | 76.36% | Reject |

The environments use different dependency versions, recorded in each rehearsal report; their trained XGBoost results are reported separately. In the native run, XGBoost improved error on 19 of the 36 series and worsened it on 17. Neither run shows an overall XGBoost improvement. This small file-order subset is not representative of the full M5 hierarchy. These are development results, not competition scores or cloud execution results.

## What the application does

- **Demand forecast:** the chart displays 28 recent observed days and a 28-day forward forecast, with a seasonal comparator and approximate uncertainty band. The API retains 84 observed days for inspection.
- **Historical replay:** an explicit count of observed days; advancing the replay date reveals one additional day to the model. Historical dates refer to the dataset, not today's calendar.
- **Model comparison:** a four-week same-weekday mean versus a fitted XGBoost Poisson model with lag, rolling-statistic and calendar features.
- **Inventory simulation:** fixed-quantity ordering, forecast order-up-to, and a buffered policy, with configurable initial stock, lead time, review period and cost assumptions.
- **Release demonstration:** using the current dataset, a deliberately degraded candidate fails a measured holdout baseline gate and a baseline clone passes at parity. It writes an illustrative local audit log and does not promote a trained model, change the application model or claim improvement.
- **Operations:** inspect replay progress, the last verified seasonal checkpoint, input quality and persisted audit events. An isolated local recovery drill exercises invalid input, interrupted publication, idempotent retry and rollback with forecast hashes as evidence.
- **Sign-in and permissions:** Viewer access for inspection and Planner access for simulations and local demonstrations, enforced by the API. Cognito hosted sign-in and JWT authorization are prepared in Terraform; the local role selector is explicitly a preview.

See [the operations guide](docs/operations.md) for snapshot validation, local recovery commands and the distinction between local checks and verified AWS execution.

![SupplySight operations and recovery evidence using labelled synthetic data](docs/images/operations-demo.png)

## Evaluation and interpretation

Every model origin uses only preceding observations. For each selected replay date, two earlier 28-day windows calibrate residual bands; three later disjoint 28-day windows score forecast error and coverage. The final model fits through the replay cutoff. XGBoost recursively uses its own earlier predictions for unavailable future lags. Hyperparameters are fixed rather than tuned on the displayed test windows.

WAPE is pooled absolute error divided by total observed sales over the three scored windows. MAE is mean absolute error in units. Bias is signed prediction error divided by observed sales; positive values mean overprediction. WAPE and bias display as unavailable for zero-demand windows. These are per-series metrics, **not the competition's official hierarchical WRMSSE**.

The chart's central forecast is expected sales (the API retains the `p50` field name for its common response schema). Lower and upper bands use empirical 10th/90th-percentile signed residuals and are constrained to contain the point forecast. They are approximate intervals, not guaranteed conditional quantiles; coverage is measured on later windows and may differ from 80%.

Inventory uses historical observed sales as a demand proxy. It accounts for on-order stock, receives zero-lead-time orders before sales, and treats shortages as lost sales. Comparative cost includes end-of-day holding cost, lost-sales penalties and order fees. Procurement spend is reported separately, so changing unit cost does not change comparative cost. Forecasts repeat beyond day 28 when a policy needs a longer planning horizon; remaining stock and pipeline orders are reported without terminal salvage value.

All savings are **simulation results**, not realized business savings. The best simulated policy is selected after scoring. Real demand may exceed recorded sales when shelves were empty; this dataset cannot establish that missing demand. There is no capacity, expiry, minimum order quantity or random lead-time model yet.

## CLI and tests

```powershell
./.venv/Scripts/python.exe -m retail_forecast forecast --store CA_1 --item HOBBIES_1_001 --model xgboost
./.venv/Scripts/python.exe -m retail_forecast release-demo
./.venv/Scripts/python.exe -m unittest discover -s tests -v
node --check frontend/app.js
node --check frontend/auth.js
node --test tests/frontend_auth.test.mjs tests/frontend_access.test.mjs
```

Forecast exports exclude held-out future actuals. Tests cover leakage boundaries, chronological splits, dataset validation, stock flows, edge cases, API input validation and publication safeguards. Optional XGBoost tests skip when that dependency is absent; use the ML extra to run them.

Authentication checks cover denied anonymous/viewer actions, session expiry, CSRF protection and verified cloud claim boundaries. The current suite passes 129 Python tests on Windows and Linux, 22 frontend tests and four offline infrastructure tests. Frontend behavior tests require Node.js 22 or newer; the website itself still needs no Node.js runtime or build. These local checks do not verify a deployed identity provider.

Rehearse the ML job stages against your imported M5 archive before creating cloud resources:

```powershell
./.venv/Scripts/python.exe -m aws.local_validate --dataset data/dataset.json --cutoff 1913 --output build/local-rehearsal
```

This executes the actual preparation, training and evaluation functions for seasonal and XGBoost candidates, reloads each portable `model.tar.gz` and checks 28-day inference with model fitting prohibited. It checks the held-out label boundary and compares three replenishment policies under default, immediate-delivery and seven-day-delivery assumptions. The measured release decisions and checks appear in `build/local-rehearsal/report.json`. Choose a new or empty output directory for each run; existing artifacts are never removed automatically. `--cutoff` can be omitted to leave the final 28 days as the holdout.

All rehearsal outputs remain local and ignored by Git. No AWS resources or credentials are used. AWS Model Registry, Batch Transform and DynamoDB publication still require live validation. Rejected candidates are inferred locally for inspection only; the AWS release gate would skip their batch and publication. Passing seasonal parity does not establish improved forecast accuracy.

Both the native and Linux 36-series M5 rehearsals passed their checks for 72 unique forecasts and 972 policy simulations each. Their measured gates rejected XGBoost because pooled WAPE exceeded the seasonal baseline, then passed the seasonal candidate at baseline parity. These are actual trained-model gate results, separate from the model lab's illustrative degraded-candidate/baseline-clone exercise.

The Linux image's `/ping` and `/invocations` HTTP routes were also checked against all 72 forecasts from each trained artifact set. Serving matched each artifact's expected output exactly, omitted held-out actuals and rejected invalid requests. This verifies the custom inference protocol locally; it does not replace a SageMaker Batch Transform run.

## AWS architecture

```mermaid
flowchart LR
  Source[M5 archive in S3] --> Ingest[EventBridge → ingestion Lambda]
  Ingest --> Snapshot[Versioned S3 snapshot]
  Snapshot --> Quality{Snapshot quality}
  Quality -->|Pass| Pipeline[SageMaker Pipeline]
  Quality -->|Reject| Quarantine[Quarantine report · previous forecast retained]
  Pipeline --> Prep[Prepare → train → backtest]
  Prep --> Gate{Release checks}
  Gate -->|Reject| Rejected[Model Registry: Rejected]
  Gate -->|Pass| Approved[Model Registry: Approved]
  Approved --> Batch[SageMaker Batch Transform]
  Batch --> Publish[Publish completed batch]
  Publish --> Results[DynamoDB]
  Results --> API[API Gateway + Lambda]
  API --> UI[S3 + CloudFront frontend]
  UI --> SignIn[Cognito hosted sign-in]
  SignIn --> Auth[JWT validation + assigned role]
  Auth --> API
  Prep -. optional experiments .-> ClearML[ClearML on EC2 + S3 artifacts]
```

Terraform defines storage, delivery, IAM roles, compute configuration, logging, and the optional experiment server. SageMaker executes training and batch inference; the API reads materialized results instead of training on a request. The replay cursor advances only after a complete successful publication. Schedules are disabled by default.

A newly deployed foundation has no published forecasts. The frontend checks `/api/health`, identifies AWS mode and shows **Awaiting the first forecast** for the expected empty catalog response. Store selections and inventory comparisons become available after a complete batch is published. AWS release history can be empty, and the local release demonstration is disabled there.

Follow [docs/aws.md](docs/aws.md) for packaging, configuration, deployment order, CI/CD, ClearML and operational limitations. Read and review a Terraform plan before creating billable AWS resources. The local synthetic dataset is suitable for smoke testing infrastructure, not for reporting M5 performance.

Confirm the regional quotas and provisioning-role permissions in the deployment runbook before deploying. See [the API reference](docs/api.md) for response schemas and publication safeguards.

## Project map

| Path | Purpose |
|---|---|
| `frontend/` | Responsive application, SVG charts and PKCE sign-in/API client |
| `retail_forecast/auth.py` | Role authorization, verified cloud claims and local sessions |
| `retail_forecast/data.py` | Synthetic generator and M5 importer |
| `retail_forecast/forecast.py` | Training, portable model artifacts, inference and backtests |
| `retail_forecast/inventory.py` | Replenishment simulation |
| `retail_forecast/api.py`, `storage.py` | Shared API contract and local/DynamoDB repositories |
| `retail_forecast/quality.py`, `operations.py` | Snapshot gate, verified checkpoints, audit journal and isolated recovery drill |
| `aws/` | Lambda handlers, SageMaker pipeline and job/image entry points |
| `infra/` | Terraform infrastructure |
| `tests/` | Automated behavior and integration checks |

## Next production milestones

Validate Cognito sign-in and one AWS replay end to end, then expand genuine M5 evaluation beyond the documented subset. Before broader use, add representative global modeling and hierarchy-aware evaluation, cost/load testing, drift monitoring and tests on actual operational inventory data. All deployed application infrastructure can stay in AWS; local development is intentionally independent.

References: [SageMaker Pipelines](https://docs.aws.amazon.com/sagemaker/latest/dg/pipelines.html), [batch inference](https://docs.aws.amazon.com/sagemaker/latest/dg/batch-transform.html), [model approval](https://docs.aws.amazon.com/sagemaker/latest/dg/model-registry-approve.html), and [M5 data](https://www.kaggle.com/competitions/m5-forecasting-accuracy/data).
