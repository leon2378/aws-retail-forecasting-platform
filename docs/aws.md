# AWS deployment

The region is **ap-southeast-2 (Sydney)**. A temporary OrderFlow deployment was tested on **10 October 2026**, then backed up and removed. There is no active hosted deployment. Its live results and remaining gaps are recorded in [verification](verification.md); a future deployment needs its own reviewed plan and acceptance checks.

## Service boundaries

| Responsibility | AWS service |
|---|---|
| Website delivery | S3 and CloudFront |
| User identity | Cognito |
| Request authentication and routing | API Gateway |
| Application and background execution | Lambda |
| Orders, inventory, effects, work and audit state | DynamoDB |
| Work delivery and dead letters | SQS |
| Isolated recovery workflow | Step Functions Standard |
| Observability | CloudWatch |
| Spending and failure notifications | AWS Budgets and SNS, when configured |

The application does not require SageMaker training, processing or Batch Transform quotas. Verify the quotas, permissions and account access needed by the implemented services before deployment. A service appearing in the AWS console does not prove this account can provision it.

## Review before provisioning

1. Confirm the intended account, region and provisioning role using read-only checks.
2. Read the infrastructure variables and inspect the complete Terraform plan. Keep credentials, email recipients and state private.
3. Configure the budget and failure notification recipient, and decide the intended retention of data and logs.
4. Review IAM scope, hosted identity configuration, API authorization, website restrictions and worker retry/dead-letter settings.
5. Proceed with cloud creation only after an explicit deployment decision. Keep optional schedules off initially.

After a new deployment, validate hosted sign-in and each role boundary, an end-to-end simulated order, competing stock reservations, duplicate API submissions and recovery. The temporary deployment exercised those paths. Natural queue redelivery/physical dead-letter arrival, publisher-failure recovery, operational alert inbox receipt and a replacement-table restoration remain separate checks; do not infer them from successful local tests.

## Prepare a reviewable plan

Install the optional AWS SDK for adapter tests, packaging or future deployment work:

```powershell
python -m pip install -e ".[aws]"
python -m aws.package --output build
terraform -chdir=infra init
terraform -chdir=infra fmt -check
terraform -chdir=infra validate
terraform -chdir=infra plan -out=../build/orderflow.tfplan
```

The package command builds allowlisted archives locally. Terraform `init` downloads providers; `plan` can read the configured AWS account but does not create resources. Inspect the saved plan before any apply. Do not execute a deployment from these instructions until the intended account and changes have been approved for the current work.

After an approved deployment, `workspace_table` identifies the database and `frontend_bucket`, `distribution_id` and `website_url` identify the hosted site. Initial catalog seeding is an explicit cloud write, separate from deployment:

```powershell
python -m aws.bootstrap --table "TABLE_NAME_FROM_TERRAFORM_OUTPUT" --region ap-southeast-2
```

Replace the placeholder with the reviewed output from the intended stack. This command creates synthetic catalog state if absent and preserves an already initialized workspace. It is not part of local setup. The temporary deployment was explicitly seeded before its hosted tests.

## Activate an approved deployment

Creating infrastructure alone leaves the website bucket empty and the database uninitialized. With delivery automation disabled, publish the assets explicitly after the reviewed apply succeeds. Keep `window.API_BASE` empty in `frontend/config.js` for the same-origin CloudFront API route. A separate API origin would also require a reviewed CORS change.

Run these commands with the approved provisioning profile and Sydney region. `terraform output` must refer to that deployed stack:

```powershell
$env:AWS_PROFILE = "YOUR_APPROVED_DEPLOYMENT_PROFILE"
$env:AWS_REGION = "ap-southeast-2"
$stack = terraform -chdir=infra output -json | ConvertFrom-Json
if ($LASTEXITCODE -ne 0) { throw "Could not read the deployed stack outputs." }
aws s3 sync frontend/ ("s3://" + $stack.frontend_bucket.value + "/") --cache-control "max-age=300"
if ($LASTEXITCODE -ne 0) { throw "Website upload failed." }
aws cloudfront create-invalidation --distribution-id $stack.distribution_id.value --paths "/*"
if ($LASTEXITCODE -ne 0) { throw "Website invalidation failed." }
python -m aws.bootstrap --table $stack.workspace_table.value --region ap-southeast-2
if ($LASTEXITCODE -ne 0) { throw "Catalog initialization failed." }
```

The upload does not delete existing objects. Only the published frontend assets belong in the website bucket; databases, backups, credentials, source archives and Terraform state remain private. Wait for CloudFront deployment and any invalidation to finish, then open the stack's `website_url`. Its public `/api/health` and `/api/auth/config` routes should identify OrderFlow and Cognito. Application data should return 401 without sign-in.

Create the owner's staff identity in the Cognito console under the deployed `orderflow-users` pool. Choose a private temporary password and suppress the invitation message if no email is intended. Assign the identity to the `admin` group; Terraform intentionally creates no passwords or staff identities. Complete the temporary-password change through the hosted site. Add separate `viewer` and `operator` identities when exercising permission boundaries. Send invitations only to intended, explicitly authorized recipients.

Confirm the configured SNS subscription from its confirmation email and deliberately test operational alert delivery. Budget notifications are separate from the SNS operational subscription. Record a hosted order progressing automatically through its simulated payment and shipment, duplicate submission, forbidden Viewer action, failed fulfillment recovery and asynchronous drill evidence. These functional paths passed in the temporary deployment. Its subscription was confirmed and a manual alarm-state test successfully executed the SNS action; operational alert inbox receipt is not yet confirmed. Idle AWS workspaces pause automatic data reads; use Refresh to inspect another user's changes.

If an outgoing record remains unpublished after stream retries are exhausted, inspect its order and the outbox-failure queue before repair. An authorized operator can invoke only the deployed `orderflow-outbox` function with `{}` to republish pending records. Its handler sends pending work and reuses the durable order guards; it does not remove historical dead-letter messages. Keep the periodic repair schedule disabled until this recovery path and its alerts have been verified.

Keep private values in ignored `infra/terraform.tfvars`. The defaults leave `schedule_enabled`, `enable_delivery` and email `monitoring_enabled` false. `api_enabled`, `worker_enabled` and `outbox_enabled` default to true. During an incident, `api_enabled=false` sets API stage throttling to zero, `worker_enabled=false` pauses queue consumption and `outbox_enabled=false` pauses stream publication. Review the intended stack's plan before applying these controls, and allow already-running functions to finish. All three controls were exercised during the post-test shutdown; apply them together so an incident plan does not reopen ingress.

When enabling delivery, the prepared CodePipeline uses an uploaded source archive and a deployment approval stage. An uploaded archive is not evidence that deployment succeeded. CI validation and actual AWS deployment remain separate checks.

No EC2, NAT gateway, ECR repository or SageMaker service is required by this implementation. DynamoDB deletion protection is enabled, and buckets do not permit automatic content destruction. Teardown therefore needs an explicit reviewed deletion-protection change and safe removal of only the intended stack's bucket objects/versions before final deletion.

The local SQLite implementation serializes writes in a transaction. The DynamoDB adapter stores entities as separate items and uses a global revision condition to serialize mutations. It deliberately trades throughput for straightforward consistency in a small demonstration. A live last-unit reservation race passed during the temporary deployment; it does not establish throughput under sustained contention. Publisher interruption between storage and queue publication still needs a deployed failure exercise.

## Storage and retention limits

The local engine permits at most 250 retained orders; the AWS adapter admits at most 100. Both keep the most recent 1,000 workspace audit events and 20 drill reports, with 60 recent timeline entries per order. The AWS adapter additionally refuses more than 2,000 entities, 3 MB of workspace state, 320 KB for one entity or 100 operations in one transaction. Entity and workspace limits apply to orders, audit records, effects, work, request keys, outgoing events and drill reports combined.

Before committing a new order, the AWS admission check reserves entity and byte headroom for unfinished work, effect/outgoing records, bounded audit growth and recovery reports. The revision condition makes this admission check atomic with the reservation. Size and headroom checks can stop new orders before the 100-order cap. The new-order cap does not prevent an identical request replay or progression of existing orders; those operations remain subject to the absolute storage and transaction limits. Capacity failure is visible rather than silently dropping orders. There is no automatic order deletion or archival command.

Snapshots read the bounded workspace and concurrent mutations contend on its revision. This is not a high-throughput, multi-tenant store. Larger deployments need entity-specific access patterns, concurrency conditions, archival and independently tested limits. The audit retention is a demo convenience, not a compliance-grade immutable archive.

The post-test shutdown removed all 82 managed resources. Read-only verification found no remaining OrderFlow managed resources across 22 checked categories and no billable user-created DynamoDB backups. One automatic SYSTEM backup remains for up to 35 days at no additional cost. Historical metric/workflow records can remain without active producers. [DynamoDB backup pricing](https://aws.amazon.com/dynamodb/pricing/)

Read [operations](operations.md) for safe teardown and [costs and alerts](costs-and-alerts.md) for billing boundaries. No production load, service-level objective or real payment/shipping integration has been verified yet. Resource absence does not establish an immediately finalized bill because billing data can lag.
