# Operations, snapshot checks and recovery

SupplySight's Operations view provides evidence about forecast availability, input quality and recovery. Local checks and drills are labelled as local execution. They do not establish cloud uptime, AWS recovery times or improved forecast accuracy.

## Inspect the workspace

Start the local server and open the **Operations** tab. The view is independent of store and product selection and remains accessible before a first AWS forecast is published.

Sign in first. Viewer can inspect Operations; Planner can run the local recovery action. The local role selector is an access preview, and Python commands run with the workspace owner's access. See [the sign-in guide](authentication.md).

The dashboard reports the accepted snapshot, seasonal checkpoint, publication time, replay cutoff, expected replay progress, quality checks and recent audit events. Historical source dates and execution timestamps have different meanings: M5 observations from 2016 are not automatically stale. Replay lag compares expected and published historical days.

The dashboard does not invent latency, availability or completed AWS job statistics. An unverified or pending cloud execution is identified as such. Cloud access is read-only; the recovery action is disabled in AWS mode.

Export the local status:

```powershell
./.venv/Scripts/python.exe -m retail_forecast operations --data-dir data --output build/operations-status.json
```

## Validate a snapshot

The quality gate accepts the normalized JSON produced by the M5 importer or synthetic generator. It checks the structure, supported source classification, calendar range, store/product identities, day counts and finite, nonnegative whole sales. An accepted snapshot can define the expected product set so accidentally missing products are blocked.

Changes in observed sales volume are warnings for review. Legitimate seasonality or promotions do not automatically make a snapshot invalid. Shift checks use only observations available at the requested cutoff, without consulting later scoring labels. Source labels describe the supplied input; validation does not establish dataset authenticity. These volume checks are simple heuristics and do not constitute a calibrated drift detector.

```powershell
./.venv/Scripts/python.exe -m retail_forecast validate-snapshot data/dataset.json --cutoff 1913 --output build/snapshot-quality.json
```

For a subsequent arrival, compare its identities with the accepted snapshot:

```powershell
./.venv/Scripts/python.exe -m retail_forecast validate-snapshot build/candidate.json --expected data/dataset.json --cutoff 1913 --output build/candidate-quality.json
```

A failed gate writes a report and exits with status 1. It does not start training. Existing artifacts are not replaced by an invalid candidate. Keep candidate data and reports private; `data/` and `build/` are ignored by Git. Use a cutoff compatible with your imported dataset; 1,913 is specific to the documented M5 evaluation subset.

The gate also runs during AWS ingestion and preparation. Invalid arrivals retain the published catalog and committed replay cursor, record a bounded quality report, and do not start a SageMaker execution. Cloud orchestration still requires live verification before schedules are enabled.

Routine arrivals must retain the accepted source, start date and product identities. Appended days are allowed; shrinking history is blocked. An explicit `import-m5` command approves a new contract, so switching from the synthetic demo to M5 remains possible. Imports pass the gate and create a verified checkpoint before atomically replacing the source file; earlier checkpoints and audit events are retained.

This scoped in-memory workflow limits snapshots to 16 MiB and one million daily observations. The documented 36-series subset fits those limits. Processing the full M5 series requires a streaming or partitioned ingestion path. Start AWS runs through the ingestion handler: a direct manual pipeline invocation checks structural quality in preparation but does not compare its source against the accepted catalog contract.

## Run the recovery drill

Choose **Run recovery drill** in Operations, or run:

```powershell
./.venv/Scripts/python.exe -m retail_forecast recovery-drill --data-dir data --output build/recovery-drill.json
```

The drill creates an isolated private copy of the current dataset. It exercises real local forecast artifacts and persisted publication state:

1. Publish and verify a complete seasonal baseline batch.
2. Submit invalid input and confirm the quality gate blocks publication.
3. Interrupt a valid batch before its commit and confirm partial results remain unavailable.
4. Retry the same run safely and confirm it is committed once.
5. Restore the previous verified checkpoint and compare its forecast hashes before and after recovery.

The result records each check, audit events and measured local recovery time. The working dataset and model-release demonstration are preserved. Deliberately modified drill input is fault injection, not additional authentic M5 data or an accuracy experiment.

Run histories, accepted datasets, quarantine reports and checkpoint artifacts remain under the local data directory's `operations/` folder. These files contain private data and are excluded from source control. Restart verification checks artifact integrity; a saved success label alone does not prove a valid checkpoint exists.

A workspace file lock prevents the server and a separate command from writing operations state simultaneously. If another writer is active, retry after it completes. The dashboard reloads the persisted audit journal when refreshed.

On 8 October 2026, the 36-series M5 drill passed in native Windows and the Linux image. Both runs passed all four recovery proofs, preserved their working source files, and restored matching forecast hashes. The complete 109-test suite also passed on both platforms. Reports stay private under `build/`; the repository screenshot uses labelled synthetic data.

## Evidence required before cloud readiness claims

Repeat one complete approved AWS replay, verify an invalid arrival starts no ML job, and perform a controlled failure/recovery exercise against the deployed environment. Attach measured outcomes and timestamps to the incident report. Local drill timings are not AWS service-level objectives or recovery guarantees.

Keep schedules and optional experiment infrastructure disabled during those checks. Review the deployment plan and cost controls separately; none of the local commands above creates AWS resources.
