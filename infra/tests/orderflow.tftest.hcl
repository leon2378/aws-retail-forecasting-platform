mock_provider "aws" {
  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012", arn = "arn:aws:iam::123456789012:root" }
  }
  mock_data "aws_partition" { defaults = { partition = "aws" } }
  mock_data "aws_cloudfront_cache_policy" { defaults = { id = "11111111-1111-1111-1111-111111111111" } }
  mock_data "aws_cloudfront_origin_request_policy" { defaults = { id = "22222222-2222-2222-2222-222222222222" } }
  mock_data "aws_cloudfront_response_headers_policy" { defaults = { id = "33333333-3333-3333-3333-333333333333" } }
  mock_resource "aws_dynamodb_table" {
    defaults = { arn = "arn:aws:dynamodb:ap-southeast-2:123456789012:table/orderflow-workspace", stream_arn = "arn:aws:dynamodb:ap-southeast-2:123456789012:table/orderflow-workspace/stream/2026-10-09T00:00:00.000" }
  }
  mock_resource "aws_sqs_queue" {
    defaults = { arn = "arn:aws:sqs:ap-southeast-2:123456789012:orderflow-queue", id = "https://sqs.ap-southeast-2.amazonaws.com/123456789012/orderflow-queue" }
  }
  mock_resource "aws_iam_role" { defaults = { arn = "arn:aws:iam::123456789012:role/orderflow-test" } }
  mock_resource "aws_lambda_function" {
    defaults = { arn = "arn:aws:lambda:ap-southeast-2:123456789012:function:orderflow-test", invoke_arn = "arn:aws:apigateway:ap-southeast-2:lambda:path/2015-03-31/functions/arn:aws:lambda:ap-southeast-2:123456789012:function:orderflow-test/invocations" }
  }
  mock_resource "aws_cloudwatch_log_group" { defaults = { arn = "arn:aws:logs:ap-southeast-2:123456789012:log-group:/aws/lambda/orderflow-test" } }
  mock_resource "aws_s3_bucket" {
    defaults = { arn = "arn:aws:s3:::orderflow-test", bucket_regional_domain_name = "orderflow-test.s3.ap-southeast-2.amazonaws.com" }
  }
  mock_resource "aws_sns_topic" { defaults = { arn = "arn:aws:sns:ap-southeast-2:123456789012:orderflow-operations" } }
  mock_resource "aws_sfn_state_machine" { defaults = { arn = "arn:aws:states:ap-southeast-2:123456789012:stateMachine:orderflow-recovery" } }
  mock_resource "aws_cloudwatch_event_rule" { defaults = { arn = "arn:aws:events:ap-southeast-2:123456789012:rule/orderflow-outbox-repair" } }
  mock_resource "aws_codebuild_project" { defaults = { arn = "arn:aws:codebuild:ap-southeast-2:123456789012:project/orderflow-test" } }
  mock_resource "aws_cloudfront_distribution" {
    defaults = { domain_name = "orderflow-example.cloudfront.net", arn = "arn:aws:cloudfront::123456789012:distribution/EXAMPLE" }
  }
  mock_resource "aws_cognito_user_pool" { defaults = { id = "ap-southeast-2_Example" } }
  mock_resource "aws_cognito_user_pool_client" { defaults = { id = "exampleclient123456789" } }
  mock_resource "aws_apigatewayv2_api" {
    defaults = { api_endpoint = "https://example.execute-api.ap-southeast-2.amazonaws.com", execution_arn = "arn:aws:execute-api:ap-southeast-2:123456789012:example" }
  }
}

run "safe_defaults_and_auth" {
  command = apply
  assert {
    condition     = aws_cloudwatch_event_rule.repair.state == "DISABLED" && length(aws_budgets_budget.monthly) == 0 && length(aws_codepipeline.app) == 0
    error_message = "Schedules, email spending alerts and delivery must require explicit opt-in."
  }
  assert {
    condition     = aws_dynamodb_table.workspace.point_in_time_recovery[0].enabled && aws_dynamodb_table.workspace.deletion_protection_enabled && aws_dynamodb_table.workspace.stream_enabled
    error_message = "Durable storage must have PITR, deletion protection and the outbox stream."
  }
  assert {
    condition     = alltrue([for bucket in aws_s3_bucket.app : bucket.force_destroy == false]) && alltrue([for block in aws_s3_bucket_public_access_block.app : block.block_public_policy && block.block_public_acls && block.restrict_public_buckets && block.ignore_public_acls])
    error_message = "Buckets must remain private and must not silently discard data on destroy."
  }
  assert {
    condition     = toset(keys(aws_apigatewayv2_route.public)) == toset(["GET /api/health", "GET /api/auth/config"]) && aws_apigatewayv2_route.api.authorization_type == "JWT" && contains(aws_apigatewayv2_route.api.authorization_scopes, "orderflow/read")
    error_message = "Only liveness and public auth settings may bypass Gateway JWT authentication."
  }
  assert {
    condition     = !aws_cognito_user_pool_client.web.generate_secret && aws_cognito_user_pool_client.web.allowed_oauth_flows == toset(["code"]) && aws_cognito_user_pool.app.admin_create_user_config[0].allow_admin_create_user_only && toset(keys(aws_cognito_user_group.roles)) == toset(["viewer", "operator", "admin"])
    error_message = "Use invite-only users, recognized roles and a public code-flow client."
  }
  assert {
    condition     = alltrue([for route in aws_apigatewayv2_route.write : route.authorization_type == "JWT" && contains(route.authorization_scopes, "orderflow/write")])
    error_message = "Mutating routes require the write scope in addition to Lambda role enforcement."
  }
  assert {
    condition     = aws_cloudfront_distribution.app.ordered_cache_behavior[0].cache_policy_id == data.aws_cloudfront_cache_policy.api.id && aws_cloudfront_distribution.app.ordered_cache_behavior[0].origin_request_policy_id == data.aws_cloudfront_origin_request_policy.api.id && aws_cloudfront_origin_access_control.web.signing_behavior == "always"
    error_message = "Authenticated API responses must not be cached and S3 access must use OAC."
  }
  assert {
    condition     = contains(aws_lambda_event_source_mapping.work.function_response_types, "ReportBatchItemFailures") && aws_lambda_event_source_mapping.work.scaling_config[0].maximum_concurrency == 2 && aws_sqs_queue.work.visibility_timeout_seconds >= 6 * aws_lambda_function.background["worker"].timeout && jsondecode(aws_sqs_queue.work.redrive_policy).maxReceiveCount == 5
    error_message = "SQS processing requires partial failures, bounded concurrency, visibility headroom and a DLQ."
  }
  assert {
    condition     = aws_lambda_event_source_mapping.outbox.destination_config[0].on_failure[0].destination_arn == aws_sqs_queue.stream_failures.arn && contains(aws_lambda_event_source_mapping.outbox.function_response_types, "ReportBatchItemFailures")
    error_message = "Discarded stream records must be recoverable and failed records must be retried."
  }
  assert {
    condition     = aws_lambda_event_source_mapping.outbox.batch_size <= 10 || aws_lambda_event_source_mapping.outbox.maximum_batching_window_in_seconds >= 1
    error_message = "A stream batch larger than ten records requires a batching window of at least one second."
  }
  assert {
    condition     = aws_sfn_state_machine.recovery.type == "STANDARD" && jsondecode(aws_sfn_state_machine.recovery.definition).States.CheckEvidence.Choices[0].BooleanEquals
    error_message = "Recovery must use a Standard workflow and reject failed evidence."
  }
  assert {
    condition     = alltrue([for role in aws_iam_role_policy.functions : !strcontains(role.policy, "dynamodb:Scan") && !strcontains(role.policy, "dynamodb:*")])
    error_message = "Runtime roles must use scoped entity operations, not account-wide table access."
  }
  assert {
    condition     = alltrue([for role in aws_iam_role_policy.functions : contains(jsondecode(role.policy).Statement[0].Action, "dynamodb:PutItem") && contains(jsondecode(role.policy).Statement[0].Action, "dynamodb:UpdateItem") && jsondecode(role.policy).Statement[0].Condition["ForAllValues:StringEquals"]["dynamodb:LeadingKeys"][0] == "WORKSPACE"])
    error_message = "Transactions require underlying item permissions, restricted to the workspace partition."
  }
}

run "pause_background_processing" {
  command = apply
  variables {
    worker_enabled = false
    outbox_enabled = false
    api_enabled    = false
  }
  assert {
    condition     = !aws_lambda_event_source_mapping.work.enabled && !aws_lambda_event_source_mapping.outbox.enabled && aws_cloudwatch_event_rule.repair.state == "DISABLED" && aws_apigatewayv2_stage.api.default_route_settings[0].throttling_burst_limit == 0 && aws_apigatewayv2_stage.api.default_route_settings[0].throttling_rate_limit == 0
    error_message = "The reviewed pause configuration must stop API ingress, consumers and scheduled repair."
  }
}

run "opt_in_notifications" {
  command = apply
  variables {
    monitoring_enabled = true
    alert_email        = "alerts@example.invalid"
    monthly_budget_usd = 10
  }
  assert {
    condition     = length(aws_sns_topic_subscription.email) == 1 && length(aws_budgets_budget.monthly) == 1 && !aws_budgets_budget.monthly[0].cost_types[0].include_credit && aws_budgets_budget.monthly[0].limit_amount == "10"
    error_message = "Reviewed opt-in must add a USD spending warning excluding credits and a confirmation-based email subscription."
  }
  assert {
    condition     = length(aws_cloudwatch_metric_alarm.publication.alarm_actions) == 1 && length(aws_cloudwatch_metric_alarm.dlq["work"].alarm_actions) == 1
    error_message = "Publication failures and dead-letter backlog must notify the configured topic."
  }
  assert {
    condition     = toset(jsondecode(aws_sns_topic_policy.operations[0].policy).Statement[0].Action) == toset(["sns:GetTopicAttributes", "sns:SetTopicAttributes", "sns:AddPermission", "sns:RemovePermission", "sns:DeleteTopic", "sns:Subscribe", "sns:ListSubscriptionsByTopic", "sns:Publish"])
    error_message = "SNS topic policies must enumerate supported topic actions instead of a service wildcard."
  }
}

run "reviewed_delivery" {
  command = apply
  variables { enable_delivery = true }
  assert {
    condition     = length(aws_codepipeline.app) == 1 && aws_codepipeline.app[0].stage[0].action[0].configuration.PollForSourceChanges == "false" && aws_codepipeline.app[0].stage[2].action[0].provider == "Manual" && !aws_codebuild_project.build[0].environment[0].privileged_mode
    error_message = "Delivery must remain manual, pass an approval stage and avoid privileged containers."
  }
}

run "reject_insecure_redirect" {
  command = plan
  variables { auth_additional_callback_urls = ["http://example.com/"] }
  expect_failures = [var.auth_additional_callback_urls]
}
