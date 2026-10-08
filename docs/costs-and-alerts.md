# Spending alerts, failure notifications and shutdown

SupplySight's monitoring is prepared in Terraform and is disabled by default. Local development does not create a budget, send email, start jobs or contact AWS. Enable this configuration only as part of a reviewed Sydney deployment. The operations dashboard remains a local rehearsal; it does not report live account spend or AWS alert delivery.

## Configure the recipient privately

Edit the ignored `infra/terraform.tfvars`, using the example file for the rest of the deployment settings:

```hcl
monitoring_enabled = true
monthly_budget_usd = 10
alert_email = "your-address@example.com"

schedule_enabled = false
enable_delivery = false
enable_clearml = false
```

The real recipient belongs only in private configuration and private Terraform state, never in source files, screenshots or GitHub. The owner's current private configuration saves the supplied recipient and a $10 threshold with monitoring still disabled. Changing a local setting does not activate cloud notifications.

The amount is a monthly alert threshold in USD. It is separate from the AWS account's promotional credit balance. The budget covers the entire account, across services and regions, rather than relying on project billing tags. Existing unrelated account usage can therefore trigger it. Credits, refunds and discounts are excluded so they do not hide gross service costs; this is not a measurement of remaining credit or the net amount payable.

Actual-cost notifications use 50%, 80% and 100% thresholds: with $10 configured, they trigger when costs exceed $5, $8 and $10. A forecast notification uses 100%. Forecasts need sufficient usage history, so actual-cost notifications matter on a new account. Budgets update with delay and do not stop resources or enforce a spending cap. See [AWS budget behavior](https://docs.aws.amazon.com/cost-management/latest/userguide/budgets-managing-costs.html), [forecast and alert guidance](https://docs.aws.amazon.com/cost-management/latest/userguide/budgets-best-practices.html) and [credit/refund cost types](https://docs.aws.amazon.com/aws-cost-management/latest/APIReference/API_budgets_CostTypes.html).

## Deploy and prove delivery

Run the offline checks first:

```powershell
terraform -chdir=infra fmt -check
terraform -chdir=infra validate
terraform -chdir=infra test
```

Then follow [the deployment runbook](aws.md) to review permissions, quotas, costs and a fresh infrastructure plan. Monitoring does not bypass that review or deploy automatically. The provisioning operator also needs permission to manage Budgets, SNS, EventBridge, CloudWatch alarms and the narrowly scoped notification role. Application users do not receive those privileges.

After an intended deployment:

1. In **Billing and Cost Management → Budgets**, inspect the monthly account budget, USD amount, excluded credits/refunds and notification recipient. AWS Budgets sends its configured email notifications directly.
2. In **Amazon SNS → Topics**, find the project's operations topic in Sydney. Open the confirmation email and confirm your subscription. Pending confirmation means operational emails will not arrive.
3. In **CloudWatch → Alarms**, inspect the API and ingestion Lambda error alarms and verify their notification action points to the operations topic.
4. In **EventBridge → Rules**, inspect the SageMaker failure rules and their SNS targets. Verify the project, account and region filters, including publication jobs.
5. From SNS, publish a short, non-sensitive test message to the exact topic and verify receipt. This proves subscription delivery, not that a SageMaker failure has been detected. Before claiming end-to-end alerting, perform a controlled failure in the deployed test environment and retain its job status, alarm/rule evidence and received notification privately.

The SNS confirmation step must be completed by the email recipient. No real email or AWS notification test is performed during local development. Follow [SNS email setup](https://docs.aws.amazon.com/sns/latest/dg/sns-email-notifications.html) if confirmation or delivery is missing.

## What failure alerts cover

| Signal | Meaning | Response |
|---|---|---|
| API/ingestion Lambda `Errors` | A Lambda invocation failed; the alarms evaluate a five-minute sum | Inspect the function logs and invocation before retrying |
| Training, processing or Batch Transform `Failed` | A job with the project's known stage name failed in Sydney | Inspect that exact job and its parent pipeline |
| Publication processing job `Failed` | The publish stage failed, including incomplete batches or a DynamoDB commit error | Verify the completed marker and last served forecast before retrying |
| Pipeline execution `Failed` | The exact project pipeline failed, including errors before a job could start | Inspect failed execution steps and keep the last valid forecast |
| Alert delivery backlog | EventBridge could not publish a notification to the operations topic | Inspect the private dead-letter queue, permissions and target configuration |

The project already writes the completed marker, catalog and replay cursor in one transaction. An incomplete batch cannot become the served forecast. If the commit response is lost but the marker confirms the same run committed, publication treats that retry as successful.

Event notifications contain selected identifiers, status and time, rather than forwarding the full SageMaker event or arbitrary failure text. Alerts can be duplicated for one incident: a failed job can also fail its pipeline. Correlate by job/execution identifiers. A rejected model release is an expected gate outcome and does not imply a failed pipeline. Quarantined input, missing runs, stale forecasts and API responses handled as HTTP errors are not covered by these failure signals alone. Inspect the operations and release evidence separately.

Notification wiring does not guarantee delivery. SageMaker events can be duplicated, and budget data is delayed. A successful local plan validates configuration rather than deployed service behavior. See [SageMaker EventBridge events](https://docs.aws.amazon.com/sagemaker/latest/dg/automating-sagemaker-with-eventbridge.html).

Retryable EventBridge delivery failures have at most ten retries and a one-hour event age. Some permanent errors, such as missing target permissions, go directly to the dead-letter queue without retries. Undelivered events can enter a private queue with SQS-managed encryption and seven-day retention. The queue can contain the original event, including fields deliberately excluded from email, so restrict access and preserve any wanted incident evidence privately. The backlog alarm uses the same SNS topic; it cannot guarantee notification if that topic or email delivery is broken. It detects EventBridge-to-topic failures, not an unconfirmed or bounced email subscription. Inspect the queue and CloudWatch during a delivery investigation. See [EventBridge dead-letter behavior](https://docs.aws.amazon.com/eventbridge/latest/userguide/eb-rule-dlq.html) and [retry policy](https://docs.aws.amazon.com/eventbridge/latest/userguide/eb-rule-retry-policy.html).

## Stop active work

Use this procedure only against a deployed project. Read-only discovery can be done first; the stop commands change that deployment. Do not run them against an unrelated account or project.

1. Confirm the AWS profile, expected account, Sydney region and this deployment's Terraform state. Preserve the state until cleanup finishes. Obtain exact resource names from that state, outputs and execution metadata; a name-prefix search alone is not proof of ownership.
2. Disable the exact daily replay rule, stop manual ingestion and pause optional delivery. Record `schedule_enabled = false` in the private configuration so a later apply cannot re-enable it accidentally. A disabled schedule does not cancel an invocation already accepted by Lambda; ingestion can run for up to 120 seconds. Wait for those invocations to finish and recheck executions before deciding no work remains.

```powershell
$region = 'ap-southeast-2'
aws sts get-caller-identity
$projectName = terraform -chdir=infra output -raw pipeline_name
aws events describe-rule --name "$projectName-daily-replay" --region $region
# After checking this is the owned replay rule:
aws events disable-rule --name "$projectName-daily-replay" --region $region
```

3. List executions of the exact project pipeline. Choose each active execution explicitly, inspect its steps, then stop it. Do not start another replay while shutting down.

```powershell
aws sagemaker list-pipeline-executions --pipeline-name $projectName --region $region
$executionArn = Read-Host 'Verified active pipeline execution ARN'
aws sagemaker describe-pipeline-execution --pipeline-execution-arn $executionArn --region $region
aws sagemaker list-pipeline-execution-steps --pipeline-execution-arn $executionArn --region $region
# After verifying its account, region and project:
aws sagemaker stop-pipeline-execution --pipeline-execution-arn $executionArn --region $region
```

Stopping is asynchronous. Recheck execution status and the actual underlying jobs until they are terminal. If any verified job remains active, use its exact name with the matching command; these are alternatives for the relevant job type:

```powershell
aws sagemaker stop-training-job --training-job-name '<verified-training-name>' --region $region
aws sagemaker stop-processing-job --processing-job-name '<verified-processing-name>' --region $region
aws sagemaker stop-transform-job --transform-job-name '<verified-transform-name>' --region $region
```

Find job ARNs in execution-step metadata, and corroborate their role and artifact locations before stopping them. Training can have a termination grace period; stopping Batch Transform discards unfinished output. Do not remove roles or storage while jobs are stopping. See [pipeline stopping](https://docs.aws.amazon.com/sagemaker/latest/APIReference/API_StopPipelineExecution.html), [training stopping](https://docs.aws.amazon.com/sagemaker/latest/APIReference/API_StopTrainingJob.html) and [transform stopping](https://docs.aws.amazon.com/sagemaker/latest/APIReference/API_StopTransformJob.html).

4. If delivery was enabled, stop each verified CodePipeline execution and inspect its provider work independently. Stopping CodePipeline does not necessarily stop an already running CodeBuild build. Stop each verified active build in CodeBuild. If ClearML was enabled, stopping its EC2 instance pauses compute but leaves its disk and other retained assets. These optional services remain off in the current project configuration.

## Remove billable resources

Stopping work is not full teardown: storage, backups, logs, alarms and optional disks can remain. Preserve wanted artifacts and incident evidence privately before removal.

1. Review all owned assets. Pipeline-created batch model entries and registered model package versions are outside Terraform state. Remove only the verified child entries no longer wanted; the model package group must be empty before deletion. A model deletion does not remove its S3 artifacts or container image.
2. For each exact project S3 bucket, preserve the wanted artifacts, then use its console **Empty** operation only when the entire verified bucket's contents can be removed. The buckets are versioned and `force_destroy = false`. Current objects, noncurrent versions, delete markers and incomplete multipart uploads need review; `aws s3 rm --recursive` does not empty a versioned bucket completely.
3. Review owned ECR images, including tagged versions, and remove those no longer wanted before repository deletion. The lifecycle policy expires only untagged images; it is not full cleanup.
4. Once no active jobs/builds remain and retention decisions are complete, save and inspect a fresh Terraform destroy plan:

```powershell
terraform -chdir=infra plan -destroy -out=../build/destroy.tfplan
terraform -chdir=infra show ../build/destroy.tfplan
# Only after reviewing the exact resources and preserving wanted data:
terraform -chdir=infra apply ../build/destroy.tfplan
```

The plan and state stay private. Do not use automatic approval or targeted deletes to bypass failed storage/retention checks. Terraform handles disabling CloudFront and waiting for its changes before deletion.

5. Check for residual assets outside the state: SageMaker-created log groups, batch model entries, package versions, retained S3 objects/versions/uploads, ECR images, separate DynamoDB backups and optional EC2/EBS assets. Check the exact global CloudFront/IAM assets as well as Sydney. Preserve unrelated resources. Confirm the owned schedule is gone and no owned compute is active.
6. Review Billing and credit usage again after reporting catches up. Teardown cannot reverse earlier usage, and delayed charges are not proof that removed resources are still running. An account-wide budget removed with this stack will no longer monitor other workloads; keep a separate account budget if ongoing account monitoring is needed.

References: [S3 versioned bucket emptying](https://docs.aws.amazon.com/AmazonS3/latest/userguide/empty-bucket.html), [model deletion scope](https://docs.aws.amazon.com/sagemaker/latest/APIReference/API_DeleteModel.html), [CodeBuild stopping](https://docs.aws.amazon.com/codebuild/latest/userguide/stop-build.html) and [CloudFront deletion](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/HowToDeleteDistribution.html).
