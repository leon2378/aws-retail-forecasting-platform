# Failure rehearsals and replacement-table recovery

These tools prepare repeatable failure checks while AWS remains stopped. The local rehearsal uses the application's actual publisher, worker and order engine with an isolated SQLite database and a virtual-time queue. Queue delivery and dead-letter movement are simulations. No AWS credentials, network connection or additional Python packages are needed.

## Run the local rehearsal

From the repository directory:

```powershell
python -S -m orderflow rehearse --output build/reliability-report-001.json
```

Choose a new evidence filename each time. The command refuses an existing output, returns a nonzero status on failure and removes its temporary databases. It does not open the application's database or add test orders to the dashboard. The JSON contains individual checks, virtual queue timing, assumptions and limitations. The release build also runs this command before packaging and retains its report.

The rehearsal covers a failed queue send, recovery of durable unpublished work, an interruption after sending but before acknowledging, duplicate handling, mixed successful and failed batches, bounded domain failures, simulated visibility-timeout redelivery and dead-letter movement, malformed messages and safe recovery followed by stale delivery.

The virtual queue uses the current configuration's 180-second visibility timeout and five allowed receives. Its clock advances immediately. The order engine stops attempting fulfillment after three provider failures, while the queue model continues delivering the blocked message until its redrive threshold. These are separate counters. Actual AWS polling, timing, permissions, stream destinations, alarms and service guarantees still need a deployed test.

## Validate and compare database evidence offline

The restore checker compares all eight workspace sections: inventory, orders, work, effects, idempotency, events, outbox and drills. It validates domain relationships and the AWS adapter's storage limits. A manifest includes the state, a stable database revision and a SHA-256 fingerprint. A fingerprint detects alteration against the saved manifest; it does not authenticate who created it. Keep trusted source evidence separately.

```powershell
python -S -m aws.recovery validate build/source-workspace.json
python -S -m aws.recovery compare build/source-workspace.json build/restored-workspace.json
```

These commands are offline. They also accept a raw JSON state containing exactly the eight sections. Comparison returns zero only when the validated states match, one for a mismatch and two for invalid input or an operational error. Reports contain section counts, fingerprints, revisions and missing/extra/changed counts without customer values or entity identifiers. Use `--require-revision-match` with two manifests for the future point-in-time restore test; it also requires the metadata revisions to match. A raw state without a revision cannot pass that stricter check.

Generated snapshots contain the complete private workspace. Save them under ignored `build/` or another private backup location, keep their access restricted and exclude them from Git and public release attachments. A passing comparison establishes data equality at the chosen snapshot; it does not establish application cutover, provider reconciliation or a recovery time objective.

## Export during an explicitly authorized AWS test

This is the tool's only network operation. Run it only against an existing, reviewed deployment, using its exact Terraform table output and your private account configuration:

```powershell
python -m aws.recovery export --table SOURCE_TABLE --region ap-southeast-2 --profile PROFILE --expected-account ACCOUNT_ID --output build/source-workspace.json
python -m aws.recovery export --table REPLACEMENT_TABLE --region ap-southeast-2 --profile PROFILE --expected-account ACCOUNT_ID --output build/restored-workspace.json
```

Replace the uppercase values before running. The optional AWS SDK must be installed for export. The tool checks caller identity, table status, table ARN and primary-key schema before reading a revision-stable snapshot. It performs read-only requests and creates a new local evidence file. It cannot restore or delete a table, change permissions, enable a worker or publish queue messages. Export reads may incur AWS request charges.

Pause API admission, workers, outbox stream processing, scheduled repair and delivery triggers before recording the comparison baseline. Stop running recovery workflows for the intended stack and wait at least 105 seconds for the longest configured function invocation to finish. Confirm stable revisions and no in-flight operations. A coherent export alone does not freeze subsequent writes.

## Future temporary AWS verification

The procedures below require a separately reviewed temporary deployment, a bounded test window, synthetic data, a spending limit and verified teardown. They have not been executed by the local rehearsal.

### Publisher failure and repair

1. Save the tested commit, exact resource identifiers, runtime policy and mapping settings privately. Keep scheduled repair disabled. Choose a synthetic order and record its request key before introducing the fault.
2. Apply a reviewed, temporary deny of `sqs:SendMessage` for only the outbox function's role and work-queue ARN. Preserve permission to send stream-failure notifications. Record the rollback before introducing the fault; never deny queue sends account-wide.
3. Submit that order only after confirming the fault is active, then close further API admission. Observe the pending outbox entity and the publisher's failure record. Lambda stream partial failures use the DynamoDB sequence number. After the mapping's bounded retries or record-age limit, inspect its actual outbox-failure SQS destination. The destination contains discarded invocation metadata; do not feed that body directly to the order worker.
4. Remove the temporary deny, verify permission propagation, and invoke only the deployed outbox function with `{}` to repair committed pending work. Each invocation processes at most 100 outgoing events. Repeat if necessary and inspect a fresh stable snapshot until no pending events remain; the invocation's pending tally describes its initial snapshot. Confirm one simulated payment and shipment, a released reservation and one stock deduction. Keep the durable ledger and failed destination record as evidence until reconciled.
5. Verify rollback of the policy and the intended enabled/disabled state of every changed control. Separately test interruption after send if a reviewed fault mechanism is available; a local acknowledgment fault does not prove that live case.

### Natural queue retries and dead-letter arrival

1. Record queue depth before testing. Submit one `fulfillment_dead_letter` order and let automatic payment complete. Record its fulfillment outbox identifier.
2. Let the existing queue message retry naturally. Do not inject duplicate messages or shorten visibility to accelerate the proof. With a 180-second visibility timeout and five allowed receives, allow several minutes plus AWS polling and monitoring delay. Worker logs record `order_work_result` with the message, order, outgoing event, stage, AWS receive count and outcome; customer fields and message bodies are excluded. Record worker deliveries and domain attempts separately. Returning a partial batch failure does not make the invocation itself a Lambda `Errors` event.
3. Confirm three domain attempts leave the paid order blocked with its stock still reserved. Confirm the matching fulfillment message subsequently reaches the actual work dead-letter queue. Approximate queue-depth metrics alone do not identify the message or prove its history. Sampling a message changes its visibility and is not strictly read-only; record any inspection and do not delete it yet.
4. Recover the order through the authorized Operator action. Confirm one original payment, one shipment and one inventory deduction. A late delivery after completion must be ignored. Retain or acknowledge only the specifically reconciled dead-letter message; do not purge or bulk-redrive either queue.
5. Observe a failure-triggered alarm and confirm the configured recipient received it. The earlier labelled alarm-state test established the SNS action path but did not establish real failure detection or inbox receipt.

### Restore into a separate DynamoDB table

1. Quiesce the source as described above and export its baseline. Retain source queues and table. Record a timestamp after its last committed write and wait until DynamoDB's recovery window includes that timestamp. Inspect the actual recovery window rather than assuming the newest timestamp is immediately restorable.
2. Review a restore to a uniquely named replacement table in Sydney. Preserve the existing source; do not overwrite it or change application routing. Wait for the replacement to become `ACTIVE`, then export it and compare every section and the metadata revision against the source baseline using `--require-revision-match`. A mismatch stops the exercise and requires investigation.
3. Inspect the replacement's configuration. DynamoDB restores require separate work to re-establish settings such as streams, point-in-time recovery, tags, alarms and IAM access. The application currently binds its table name, table/stream permissions and outbox event source to the Terraform workspace resource. There is no automatic replacement-table cutover. Prepare and review those infrastructure changes independently before any application switch.
4. Reconcile queue state and durable outgoing work against the restored ledger. Existing `sent` outbox entries are not automatically republished. An older restore can also lose recorded effects that happened after its timestamp. The current providers are simulations; a real payment or shipping integration needs provider-side reconciliation before resuming.
5. Run representative read and duplicate-request checks before enabling work on a replacement. Record recovery duration only for the stages actually exercised. Keep the source and queues until verification is complete, then include the independently created replacement in the explicit backup and teardown inventory.

An exact comparison is a restore-data check. A complete recovery additionally requires reconciled work, correct infrastructure bindings and verified application behavior. The current local tools do not automate these mutations.

AWS references: [SQS partial batch responses](https://docs.aws.amazon.com/lambda/latest/dg/services-sqs-errorhandling.html), [SQS dead-letter queues](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-dead-letter-queues.html), [DynamoDB stream failure destinations](https://docs.aws.amazon.com/lambda/latest/dg/services-dynamodb-errors.html), [DynamoDB recovery window](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/PointInTimeRecovery_Howitworks.html), and [DynamoDB restore settings](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/pointintimerecovery_restores.html).
