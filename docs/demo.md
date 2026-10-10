# Two-minute OrderFlow walkthrough

Use this sequence for a portfolio recording or a live presentation. It runs entirely locally. The role selector demonstrates permissions; payments, refunds and shipping are simulations.

## Prepare a separate workspace

From the repository, use a new database filename so the demonstration preserves your existing orders:

```powershell
python -m orderflow serve --database data/showcase-001.db --port 8001
```

Open [http://127.0.0.1:8001](http://127.0.0.1:8001). A new workspace includes six synthetic orders, including one blocked fulfillment and one declined payment. Reusing a database preserves its previous state; choose a new filename for another fresh recording. Close other tabs and hide personal account details before recording.

## Recording sequence

| Time | Action | Explain |
|---|---|---|
| 0:00–0:15 | Show the sign-in screen and enter as Viewer | Staff roles have different permissions; the local selector is a demonstration |
| 0:15–0:30 | Open Orders and Recovery, then sign out | Viewer can inspect work, while changes require an authorized role |
| 0:30–1:05 | Enter as Operator, create a successful one-unit order, open Recovery and select Process work twice | Stock is reserved when the order commits; separate payment and fulfillment stages finish the order |
| 1:05–1:30 | Open the existing River & Co. blocked order, inspect its history, select Retry fulfillment, then return to Recovery and select Process work | Recovery resumes fulfillment while keeping the existing payment |
| 1:30–1:50 | Sign out, enter as Administrator, and run the recovery drill | All nine isolated checks cover duplicates, stock safety and recovery without changing workspace orders |
| 1:50–2:00 | Show the verification document | A temporary AWS deployment also passed functional checks and was removed; restoration, load and real-provider checks remain outstanding |

Inspect the completed order's history to show its recorded payment and shipment. Explain that real AWS workers process queued stages automatically; this local demonstration uses an explicit processing control. The declined Atlas Supply sample illustrates stock release after an unsuccessful payment.

Request replay and concurrent reservations are verified by the acceptance tests and isolated drill. The order form starts a new request after a successful submission, so submitting the form twice is not a demonstration of replaying the same idempotency key.

Stop the demonstration server with Ctrl+C when finished. The recording requires no AWS deployment and does not change the existing workspace on port 8000.

See [verification evidence](verification.md) for the measured local and hosted results, and [operations](operations.md) for recovery and shutdown details.
