# Operations and recovery

The local dashboard exposes order progress, inventory, pending work, retries, failed work and audit events. All payments, refunds and shipments are simulated. Operations evidence should show a specific invariant and its measured result, not simply that a button completed.

## Recovery exercise

Use the isolated recovery drill in the local application. Its temporary database does not alter the orders displayed in the workspace. Inspect the evidence for:

- a duplicate order returning the original order without another reservation;
- competing reservations never making inventory negative;
- interruption after a simulated external effect, followed by a retry that reuses that effect;
- exhausted work becoming visible as failed work;
- an authorized recovery action resuming the order without a second payment.

Keep the drill output with the release's verification evidence. A local drill is not evidence of live SQS redelivery, DynamoDB concurrency or a real commerce provider's behavior.

For an inspectable terminal report:

```powershell
python -m orderflow drill --database data/orderflow.db
```

The drill has nine checks and returns `passed`, `status`, measured duration and per-check details. It adds its evidence to the workspace but keeps test orders and inventory isolated.

Payment-decline stock release is checked separately by the acceptance suite and can be explored using the application's labelled payment-decline scenario.

## Handling failed work

Inspect the order's latest error and audit history before recovering it. A known simulated scenario can be repaired through the application's explicit recovery action. Retrying a permanently failing integration without correcting its cause is not remediation.

Cancelling an unshipped order releases stock and discards its pending outgoing work in the same transaction. Already queued messages can still arrive and are ignored by the terminal order's work guard. Cancellation cannot retract a recorded shipment.

When integrating a real provider, use the order/effect identifier to check its recorded result before submitting another request. If the provider's outcome is uncertain, reconcile it first. Operators must not create a new order merely to bypass an existing idempotency record.

In AWS, the application's failed-work record and the SQS dead-letter queue are separate views. The API's retry action repairs the simulation state and emits new outgoing work; it does not delete historical SQS dead-letter messages. Inspect, retain or acknowledge those messages separately after confirming the order recovered. A queue alarm can therefore remain active after the application's recovery has completed. Do not redrive an entire queue without checking the affected orders and provider outcomes.

## Backups and restoration

The local SQLite database contains the application's orders, reservations, work, effect outcomes and audit records. Keep all of them together. Copying just the order table can lose the information that prevents repeated effects.

Use the application's backup operation rather than copying a live SQLite file. Restore into a separate database, verify its integrity and inspect representative orders and work before choosing it for the server. Stop the server and workers before replacing an active database. Keep the previous database until the restored application has been checked.

```powershell
python -m orderflow backup data/backups/orderflow-001.db --database data/orderflow.db
python -m orderflow seed --database data/restored-orderflow.db
python -m orderflow restore data/backups/orderflow-001.db --database data/restored-orderflow.db
python -m orderflow validate --database data/restored-orderflow.db
python -m orderflow serve --database data/restored-orderflow.db --empty
```

Stop the existing server before starting this restored instance on the same port. A backup is reported verified only after SQLite integrity and domain checks pass. `restore` validates the candidate and writes a timestamped safety backup of the target before replacing its state. It refuses a missing target workspace. Use a freshly seeded target as above if the current workspace is corrupt. A successful `validate` checks domain consistency; inspect the recovered workflow as well.

Backups are private generated files and must remain outside Git. The demo database contains synthetic commerce records; that does not justify putting future customer data in source control.

Restoring an older snapshot also rolls back its recorded effects. A future real provider may have succeeded after that snapshot was taken. Reconcile those external outcomes before resuming work; local restoration cannot undo a real payment or shipment.

The AWS plan uses DynamoDB recovery controls. Live point-in-time recovery and a restore into a replacement table still need to be exercised after deployment. A recovery setting alone does not prove restoration works or establish a recovery time objective.

Use the [failure rehearsal and restore-check guide](reliability.md) to prepare those checks locally. Its isolated publisher/queue rehearsal and offline snapshot comparison do not contact AWS. The explicit export command reads an existing, identity-checked table; it cannot restore it or switch the application to a replacement. The guide documents the separate queue reconciliation and infrastructure changes needed after restoration.

## Stop charges and remove resources

The temporary Sydney deployment was tested, backed up and removed on 10 October 2026. No active OrderFlow infrastructure remains. Local commands and tests do not start AWS resources.

For a future deployment, stop new work before teardown:

1. Set `schedule_enabled=false` for scheduled outbox repair and stop CI deployment triggers.
2. Set `api_enabled=false`, `worker_enabled=false` and `outbox_enabled=false` for the intended stack, review and apply that incident plan, and inspect in-flight orders. Zero API stage throttles stop new requests; disabled event mappings stop new consumption. Stop only the stack's running recovery executions. Pausing new work does not interrupt a Lambda invocation already running, so wait at least the longest configured function timeout plus a margin.
3. Save the Terraform state, outputs, database entities, logs and workflow evidence privately. Validate the database and verify the backup's hashes. Include every S3 object version and delete marker in the bucket inventory; copying only current website files is incomplete.
4. After verifying the backup, review and apply `table_deletion_protection_enabled=false` while keeping API, workers and scheduling disabled. Review a destroy plan against the intended environment and account. Empty only the stack's explicitly identified, owned website/artifact buckets, including all backed-up versions and delete markers, then apply the reviewed destroy plan. Never use account-wide deletion.
5. Check retained resources: DynamoDB tables/backups, S3 object versions, ECR images, log groups and any independently created resources. Retention and deletion protection can intentionally leave billable data behind.
6. Confirm the stack is absent using resource inventory, then revisit billing after reporting delay. An empty Terraform state does not prove the entire account has no charges.

Cost monitoring and notification resources can also be billable or retained. Read [costs and alerts](costs-and-alerts.md) before deployment. Do not remove unrelated account resources in an effort to make the account bill zero.

The exercised shutdown archived a revision-stable snapshot and all 79 raw database entities, six log groups, workflow evidence and all six website object versions. Hash verification passed before the buckets were emptied. Terraform removed all 82 managed resources; a separate AWS inventory found all 22 checked resource categories empty and no billable user-created backups. The pre-existing deployment role was independently verified as preserved. One automatically retained DynamoDB SYSTEM backup remains for up to 35 days at no additional cost. [DynamoDB backup pricing](https://aws.amazon.com/dynamodb/pricing/)

The archive remains local and private. Database recovery into a replacement AWS table was not tested, so neither the local archive nor the retained SYSTEM backup establishes a cloud restoration time objective. Billing for the short test may appear after the resources have been removed.

## Release evidence

Before hosting the application, record the tested commit, local behavior tests, frontend check and validated infrastructure plan. After hosting, add evidence of real sign-in and role denial, an end-to-end order, retries/dead letters, recovery, alert delivery and database restoration. Run representative concurrency and load tests before making throughput or availability claims.
