# Costs and alerts

OrderFlow can run locally without cloud resources. A temporary Sydney deployment was tested and removed on 10 October 2026; no active billable OrderFlow resources remain. The serverless architecture avoids continuously running application servers, but AWS usage can still incur charges.

## What can cost money

Requests, Lambda execution, workflow transitions, queue operations, database reads/writes and storage, website storage and delivery, log ingestion/retention, backups and notification services can all contribute. Credits and free allowances are account-dependent and can be shared with other projects. Do not treat an AWS service's free allowance as an unlimited free deployment.

There is no measured cloud monthly estimate yet. Local runtime and test traffic are not evidence of real AWS usage or a production traffic forecast. Review the actual Terraform plan and estimate costs against the intended traffic before deploying.

## Before deployment

Configure an account budget and alerts using private local configuration. Keep the alert recipient out of source control. Confirm any email subscription and verify delivery deliberately after deployment; a configured address does not establish that notifications arrive.

The Terraform variables are `monitoring_enabled`, `alert_email` and `monthly_budget_usd`. Set the recipient privately in ignored `infra/terraform.tfvars`; the initial budget default is USD 10 per month. Its spending view is account-wide and excludes credits, so consuming a promotional credit still counts toward the warning. Other projects can trigger it too.

Alerts should cover spending thresholds, API failures, worker failures and dead-letter work. Choose thresholds that fit expected test traffic. Associate failure alerts with the affected environment so an unrelated account event cannot be mistaken for an OrderFlow failure.

The stack includes function error, API 5xx response, work/outbox dead-letter, queue age, recovery-workflow failure and outbox-publication failure monitoring. The queue-age warning is for messages older than ten minutes across two five-minute evaluation periods. The default budget warns after 50% and 100% actual spend and 100% forecast spend. Infrastructure existence is separate from delivery: email delivery requires monitoring to be configured and the subscription to be confirmed.

Budget and billing data are delayed. **Budget alerts do not stop jobs or cap spending.** Enforce a separate operational response: disable new ingress and consumers, inspect queued work and remove unused resources using the [shutdown procedure](operations.md).

Keep scheduling disabled until a hosted end-to-end test has passed. Do not create a continuously running worker, NAT gateway or unrelated managed service to support a small serverless demo without a concrete need and cost review.

## Exercised controls and remaining evidence

The temporary deployment verified the USD 10 monthly budget, ten configured alarms and one confirmed SNS email subscription. A clearly labelled CloudWatch test state change successfully executed the existing alarm's SNS action. This establishes the alert handoff; recipient inbox receipt and alarms triggered by injected real job/publication failures were not confirmed. The test did not force a budget threshold or establish budget-email delivery. [CloudWatch alarm testing](https://docs.aws.amazon.com/AmazonCloudWatch/latest/APIReference/API_SetAlarmState.html)

All 82 managed resources, including budgets, notifications, logs and monitoring producers, were removed after testing. Independent AWS inventory found no billable OrderFlow resources or user-created DynamoDB backups. AWS retains one automatic SYSTEM recovery backup for up to 35 days at no additional cost. The local evidence archive is outside source control. [DynamoDB backup pricing](https://aws.amazon.com/dynamodb/pricing/)

The short functional test does not establish a measured monthly bill. Final usage and credit accounting can arrive later; absence of active project resources does not establish zero account-wide charges. See [verification](verification.md) for the tested scope.
