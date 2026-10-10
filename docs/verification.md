# Verification evidence

This records the difference between behavior exercised locally and behavior still requiring a deployed environment. The application uses a synthetic catalog and simulated payments, refunds and shipping.

## Local reference, 9 October 2026

The independent acceptance suite passed 16 checks on Windows with Python 3.13 and SQLite:

| Invariant | Evidence |
|---|---|
| Inventory is never oversold | Two simultaneous request threads compete for one remaining unit; one order commits and one gets an insufficient-stock conflict |
| Concurrent duplicates reserve once | Two simultaneous identical requests return one order with one reservation and one outgoing work event |
| Keys reject changed content | Reusing an identity's key for different content returns a conflict without state mutation |
| Identities do not share request keys | Two identities using the same key create separate orders |
| Multi-product requests are atomic | One unavailable line rejects the complete request, leaving inventory and orders unchanged |
| Work survives server restart | A new engine reads the persisted reservation and work, then completes it |
| Payment declines release stock | Declined work returns the full reservation and creates no payment or shipment effects |
| Effects tolerate interrupted processing | Payment and shipment redelivery each reuse one effect; stock is deducted once |
| Stale work cannot advance another stage | Repeated payment delivery does not perform fulfillment |
| Failures are bounded and recoverable | Three fulfillment failures block the work; recovery completes it using one payment |
| Compensation is repeatable | Repeated cancellation records one refund and releases stock once |
| A committed shipment cannot be cancelled | Interrupted shipment evidence blocks cancellation until processing resumes |
| Inputs have no side effects when rejected | Boolean, fractional, negative, zero, string and excessive quantities leave the domain unchanged |
| Restoration keeps the recovery ledger | Backup restoration retains request keys, effects, reservations and work |
| Invalid backups do not replace healthy state | Corrupt work stage, effect identity and outbox identity are rejected before any live-state change |
| Retention follows time rather than identifier order | Serialized reloads retain recent audit events even when IDs sort opposite to creation time |

The isolated recovery drill also passed all nine of its checks. It exercised real local transactions and simulation effect records in a temporary database. It does not send real payments, test AWS queue delivery or prove live restoration of an AWS database.

Fifteen black-box HTTP checks also passed against ephemeral loopback servers. These exercised anonymous denial, Viewer/Operator/Admin permissions, expiry and logout, HTTP-only cookies, CSRF and origin enforcement, Host validation, malformed and oversized JSON rejection, price/request-key tampering denial, static path restrictions and an end-to-end duplicate order. They use the actual HTTP server rather than bypassing its request handling.

## Final integration, 10 October 2026

The combined Python suite passed all 73 tests, including the acceptance, HTTP, authentication, API, administration, AWS adapter and release-boundary checks. The frontend passed 28 behavior tests; desktop and phone layouts were exercised in a browser with no console errors.

Terraform formatting and validation passed, as did all five mocked infrastructure runs. Release archives passed fingerprint and private-file exclusion checks. A fresh extraction of the source archive passed its release checks, initialized a catalog and completed all nine recovery-drill checks. These checks did not contact or create AWS resources.

Additional regressions cover cancellation while outgoing work is being published, ignored late delivery after cancellation, a corrupt restore preserving both live data and a safety backup, and deeply nested JSON returning a controlled input error. The final local container check also ran the nine-check drill on Python 3.12 as a non-root user with networking disabled.

The deployment readiness review added four Linux shell checks proving that a failed archive fingerprint, function update or deployment wait stops the release before later publication. All four passed with fake AWS commands and networking disabled. The stream batch window now satisfies the documented service constraint, and all five infrastructure mock runs still pass. Thirty-five frontend tests now include idle AWS refresh suppression, continued session checks, active order/drill refresh and unchanged local behavior. Read-only account checks and a saved Terraform plan do not establish successful cloud creation or hosted operation.

## Temporary Sydney deployment, 10 October 2026

OrderFlow was deployed in ap-southeast-2, exercised with synthetic staff and orders, then backed up and removed. There is no active hosted application. Payments, refunds and shipping remained simulations throughout.

| Live verification | Observed result |
|---|---|
| API and workflow checks | All 34 checks passed using 47 HTTP requests in 61.4 seconds |
| Hosted authentication | Real Cognito code flow with PKCE worked for Viewer, Operator and Admin; anonymous requests were denied and forbidden role actions returned 403 |
| Automatic fulfillment | Committed DynamoDB outgoing work reached actual SQS/Lambda workers; one payment and one shipment completed the order |
| Request idempotency | An identical request returned the original order; changed content with the same key returned a conflict |
| Duplicate delivery | Additional real SQS work messages preserved one payment and one shipment; stale payment work did not advance fulfillment |
| Inventory race | Two simultaneous final-unit requests produced one accepted order and one stock conflict |
| Compensation | Payment decline released stock; repeated paid cancellation recorded one refund; cancelled race reservations restored the original inventory |
| Failed-work recovery | Actual workers exhausted three domain attempts; Operator recovery reused one payment and produced one shipment |
| Recovery workflow | The deployed Standard Step Functions execution succeeded and persisted all nine passing isolated drill checks |
| Infrastructure controls | All 15 checks passed, covering functions, mappings, private/versioned buckets, encryption, deletion protection, PITR configuration, scheduling and monitoring |
| Browser workflows | All 13 checks passed: three staff roles, sign-out, read-only Viewer controls, automatic Operator completion and Admin recovery evidence; no browser warnings or errors were captured |
| Alert route | The recipient confirmed the SNS subscription; a labelled CloudWatch alarm test successfully executed its SNS action |
| Backup and shutdown | Eight offline shutdown safeguards passed; quiescence preceded a verified local archive of all 79 raw database entities, object versions, logs and workflow evidence |
| Resource deletion | Terraform destroyed all 82 managed resources; separate AWS inventory verified all 22 checked categories empty and no billable user-created backups; the original deployment role remained intact |

The initial apply exposed an unsupported SNS action wildcard. The topic policy was corrected to enumerate supported actions and the repair plan changed only that policy. An infrastructure regression now checks those actions. The five mocked infrastructure runs also pass with the explicit API, worker and publisher shutdown controls.

Duplicate SQS messages accelerated the domain retry-exhaustion scenario. This demonstrates the worker's bounded domain failures and recovery, but does not establish natural visibility-timeout redelivery or physical SQS dead-letter arrival. The Step Functions proof runs isolated synthetic transactions; it is not a DynamoDB restore test.

Private outputs, state, identifiers, credentials and detailed cloud evidence remain outside source control. Temporary test passwords, access tokens and login diagnostics were removed locally; synthetic cloud identities were deleted with the stack. AWS retains one automatic DynamoDB SYSTEM backup for up to 35 days at no additional cost. Final billing for the test can be delayed. [DynamoDB backup pricing](https://aws.amazon.com/dynamodb/pricing/)

## Offline failure preparation, 10 October 2026

The expanded Python suite passed all 116 tests, including 27 restore-tool checks and ten publisher/worker rehearsal regressions. All four deployment fail-fast shell tests ran. The separate offline rehearsal passed 19 checks across five isolated scenarios using the actual publisher, worker and order engine, with real temporary SQLite transactions and a virtual-time queue.

The scenarios cover a rejected send, a lost response after queue acceptance, repair and duplicate delivery, separate domain and queue retry budgets, modeled dead-letter arrival, Operator recovery followed by stale replay, malformed transport messages and mixed-batch acknowledgment. The tests also prove that deliberately regressed publisher/worker behavior produces failed evidence. Temporary databases are removed and the application's workspace is not opened.

The rehearsal passed in a fresh source-archive extraction without the AWS SDK, and in a Python 3.12 container running as a non-root user with networking disabled. Release checks now retain a rehearsal report before packaging. These results establish local integration behavior; queue movement and timing are simulations, and no new AWS resources or account APIs were used.

The read-only restore tool validated all 78 payload entities from the earlier private database archive, alongside its metadata revision (79 original raw entities including `META`). Tests reject changed payloads, missing recovery records, corrupt fingerprints, ambiguous JSON, overwriting existing snapshots and mismatched AWS account/table selections. Optional strict revision comparison checks the restore checkpoint as well as all eight workspace sections. The archive remains private; this validation is not a database restoration.

Worker receipt logs now link message ID, order ID, stage, outgoing event, receive count and outcome while excluding customer fields and message bodies. The [failure and replacement-table runbook](reliability.md) identifies live evidence still needed and the infrastructure and queue reconciliation required for cutover.

## Remaining cloud evidence

- Hosted token expiry and wrong issuer/audience rejection.
- Publisher failure, stream-failure handling and repair of durable unpublished work.
- Natural SQS redelivery and physical dead-letter queue arrival.
- Recipient inbox receipt, real failure-triggered alerts and budget threshold notifications.
- Database restoration into a replacement table with effect reconciliation.
- Representative traffic, production concurrency, latency, service objectives and a measured billing period.

Real customer use additionally requires verified commerce integrations, provider-side idempotency and uncertain-outcome reconciliation, access and privacy policies, abuse controls, retention decisions and operational ownership. Neither a unit-test count nor an architecture diagram establishes those outcomes.
