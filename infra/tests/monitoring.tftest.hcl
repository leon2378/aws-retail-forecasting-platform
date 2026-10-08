# Mock plans never contact AWS or send email. Reserved example identities only.
mock_provider "aws" {
  override_during = plan
  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012" }
  }
  mock_data "aws_partition" {
    defaults = { partition = "aws" }
  }
  mock_resource "aws_cloudfront_distribution" {
    defaults = { domain_name = "test.example.cloudfront.net" }
  }
  mock_resource "aws_cognito_user_pool" {
    defaults = { id = "ap-southeast-2_fixture" }
  }
  mock_resource "aws_cognito_user_pool_client" {
    defaults = { id = "fixturepublicclient" }
  }
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::123456789012:role/fixture" }
  }
  mock_resource "aws_sns_topic" {
    defaults = { arn = "arn:aws:sns:ap-southeast-2:123456789012:fixture" }
  }
  mock_resource "aws_sqs_queue" {
    defaults = {
      arn = "arn:aws:sqs:ap-southeast-2:123456789012:fixture"
      url = "https://sqs.ap-southeast-2.amazonaws.com/123456789012/fixture"
    }
  }
}

variables {
  lambda_zip               = "tests/monitoring.tftest.hcl"
  schedule_enabled         = false
  enable_delivery          = false
  enable_clearml           = false
  pipeline_definition_file = ""
  monitoring_enabled       = false
  alert_email              = ""
}

run "notifications_are_opt_in" {
  command = plan
  assert {
    condition = (
      length(aws_budgets_budget.monthly) == 0 &&
      length(aws_sns_topic.operations) == 0 &&
      length(aws_sns_topic_subscription.operations_email) == 0 &&
      length(aws_sns_topic_policy.operations) == 0 &&
      length(aws_iam_role.alert_delivery) == 0 &&
      length(aws_iam_role_policy.alert_delivery) == 0 &&
      length(aws_cloudwatch_event_rule.job_failure) == 0 &&
      length(aws_cloudwatch_event_target.job_failure) == 0 &&
      length(aws_sqs_queue.alert_delivery) == 0 &&
      length(aws_sqs_queue_policy.alert_delivery) == 0 &&
      length(aws_cloudwatch_metric_alarm.alert_delivery) == 0 &&
      length(aws_cloudwatch_metric_alarm.api_errors.alarm_actions) == 0 &&
      length(aws_cloudwatch_metric_alarm.ingest_errors.alarm_actions) == 0
    )
    error_message = "The default must create no new notification resources or alarm actions. The two pre-existing Lambda alarms remain passive."
  }
  assert {
    condition     = aws_cloudwatch_event_rule.replay.state == "DISABLED"
    error_message = "Adding cost controls must never enable ingestion."
  }
}

run "account_budget_counts_usage_before_credits" {
  command = plan
  variables {
    monitoring_enabled = true
    alert_email        = "operator@example.com"
  }
  assert {
    condition = (
      aws_budgets_budget.monthly[0].account_id == "123456789012" &&
      aws_budgets_budget.monthly[0].budget_type == "COST" &&
      aws_budgets_budget.monthly[0].time_unit == "MONTHLY" &&
      aws_budgets_budget.monthly[0].limit_unit == "USD" &&
      aws_budgets_budget.monthly[0].limit_amount == "10" &&
      length(aws_budgets_budget.monthly[0].cost_filter) == 0 &&
      length(aws_budgets_budget.monthly[0].filter_expression) == 0 &&
      !aws_budgets_budget.monthly[0].cost_types[0].include_credit &&
      !aws_budgets_budget.monthly[0].cost_types[0].include_refund &&
      !aws_budgets_budget.monthly[0].cost_types[0].include_discount &&
      aws_budgets_budget.monthly[0].cost_types[0].include_tax &&
      aws_budgets_budget.monthly[0].cost_types[0].include_support &&
      aws_budgets_budget.monthly[0].cost_types[0].include_upfront &&
      !aws_budgets_budget.monthly[0].cost_types[0].use_amortized &&
      !aws_budgets_budget.monthly[0].cost_types[0].use_blended
    )
    error_message = "The budget must include account-wide gross cost rather than allow credits, refunds or tag/region filters to hide consumption."
  }
  assert {
    condition = (
      toset([for notification in aws_budgets_budget.monthly[0].notification :
        "${notification.notification_type}:${notification.threshold}"
      ]) == toset(["ACTUAL:50", "ACTUAL:80", "ACTUAL:100", "FORECASTED:100"]) &&
      alltrue([for notification in aws_budgets_budget.monthly[0].notification :
        notification.comparison_operator == "GREATER_THAN" &&
        notification.threshold_type == "PERCENTAGE" &&
        notification.subscriber_email_addresses == toset([var.alert_email]) &&
        notification.subscriber_sns_topic_arns == null
      ])
    )
    error_message = "Send direct budget emails after actual 50/80/100 percent and forecast 100 percent, without a cross-region SNS dependency."
  }
  assert {
    condition = (
      aws_sns_topic_subscription.operations_email[0].protocol == "email" &&
      aws_sns_topic_subscription.operations_email[0].endpoint == var.alert_email &&
      !aws_sns_topic_subscription.operations_email[0].endpoint_auto_confirms
    )
    error_message = "Operational emails must require recipient confirmation."
  }
}

run "job_failures_match_only_this_application" {
  command = plan
  variables {
    monitoring_enabled = true
    alert_email        = "operator@example.com"
  }
  assert {
    condition = (
      toset(keys(aws_cloudwatch_event_rule.job_failure)) == toset(["training", "processing", "publication", "transform", "pipeline"]) &&
      alltrue([for rule in aws_cloudwatch_event_rule.job_failure :
        jsondecode(rule.event_pattern).source == ["aws.sagemaker"] &&
        jsondecode(rule.event_pattern).account == ["123456789012"] &&
        jsondecode(rule.event_pattern).region == ["ap-southeast-2"] &&
        length(jsondecode(rule.event_pattern)["detail-type"]) == 1 &&
        rule.state == "ENABLED"
      ])
    )
    error_message = "Failure rules must be scoped to the current account, Sydney, SageMaker and an exact event kind."
  }
  assert {
    condition = (
      jsondecode(aws_cloudwatch_event_rule.job_failure["training"].event_pattern).detail == {
        TrainingJobName = [{ prefix = "retail-forecast-train-" }], TrainingJobStatus = ["Failed"]
      } &&
      jsondecode(aws_cloudwatch_event_rule.job_failure["transform"].event_pattern).detail == {
        TransformJobName = [{ prefix = "retail-forecast-forecast-" }], TransformJobStatus = ["Failed"]
      } &&
      jsondecode(aws_cloudwatch_event_rule.job_failure["processing"].event_pattern).detail == {
        ProcessingJobName = [{ prefix = "retail-forecast-prepare-" }, { prefix = "retail-forecast-evaluate-" }, { prefix = "retail-forecast-reject-" }], ProcessingJobStatus = ["Failed"]
      } &&
      jsondecode(aws_cloudwatch_event_rule.job_failure["publication"].event_pattern).detail == {
        ProcessingJobName = [{ prefix = "retail-forecast-publish-" }], ProcessingJobStatus = ["Failed"]
      } &&
      jsondecode(aws_cloudwatch_event_rule.job_failure["pipeline"].event_pattern).detail == {
        pipelineArn = ["arn:aws:sagemaker:ap-southeast-2:123456789012:pipeline/retail-forecast"], currentPipelineExecutionStatus = ["Failed"]
      }
    )
    error_message = "Prefixes must follow aws/pipeline.py exactly, publication must have its own non-overlapping rule, and pipeline ARN/status must use AWS's lower-camel fields. A normal model rejection is not a failure."
  }
  assert {
    condition = (
      alltrue([for target in aws_cloudwatch_event_target.job_failure :
        toset(keys(target.input_transformer[0].input_paths)) == toset(["event_id", "time", "region", "status", "job"]) ||
        toset(keys(target.input_transformer[0].input_paths)) == toset(["event_id", "time", "region", "status", "execution"])
      ]) &&
      alltrue([for target in aws_cloudwatch_event_target.job_failure :
        toset(keys(jsondecode(target.input_transformer[0].input_template))) ==
        toset(["project", "alert", "job", "status", "time", "region", "event_id", "action"]) ||
        toset(keys(jsondecode(target.input_transformer[0].input_template))) ==
        toset(["project", "alert", "job", "status", "time", "region", "event_id", "action", "execution"])
      ]) &&
      alltrue([for target in aws_cloudwatch_event_target.job_failure :
        !strcontains(target.input_transformer[0].input_template, "<aws.events.event") &&
        !contains(values(target.input_transformer[0].input_paths), "$.detail") &&
        !strcontains(target.input_transformer[0].input_template, "FailureReason")
      ]) &&
      jsondecode(aws_cloudwatch_event_target.job_failure["publication"].input_transformer[0].input_template).alert == "Forecast publication failed" &&
      !contains(keys(aws_cloudwatch_event_target.job_failure["pipeline"].input_transformer[0].input_paths), "job") &&
      aws_cloudwatch_event_target.job_failure["pipeline"].input_transformer[0].input_paths["execution"] == "$.detail.pipelineExecutionArn" &&
      jsondecode(aws_cloudwatch_event_target.job_failure["pipeline"].input_transformer[0].input_template).execution == "<execution>"
    )
    error_message = "Email payloads must allowlist operational metadata and the pipeline execution identifier; exclude full events, diagnostics and environment."
  }
  assert {
    condition = (
      alltrue([for target in aws_cloudwatch_event_target.job_failure :
        target.input_transformer[0].input_paths["event_id"] == "$.id" &&
        target.input_transformer[0].input_paths["time"] == "$.time" &&
        target.input_transformer[0].input_paths["region"] == "$.region"
      ]) &&
      aws_cloudwatch_event_target.job_failure["training"].input_transformer[0].input_paths["job"] == "$.detail.TrainingJobName" &&
      aws_cloudwatch_event_target.job_failure["training"].input_transformer[0].input_paths["status"] == "$.detail.TrainingJobStatus" &&
      aws_cloudwatch_event_target.job_failure["processing"].input_transformer[0].input_paths["job"] == "$.detail.ProcessingJobName" &&
      aws_cloudwatch_event_target.job_failure["processing"].input_transformer[0].input_paths["status"] == "$.detail.ProcessingJobStatus" &&
      aws_cloudwatch_event_target.job_failure["publication"].input_transformer[0].input_paths["job"] == "$.detail.ProcessingJobName" &&
      aws_cloudwatch_event_target.job_failure["publication"].input_transformer[0].input_paths["status"] == "$.detail.ProcessingJobStatus" &&
      aws_cloudwatch_event_target.job_failure["transform"].input_transformer[0].input_paths["job"] == "$.detail.TransformJobName" &&
      aws_cloudwatch_event_target.job_failure["transform"].input_transformer[0].input_paths["status"] == "$.detail.TransformJobStatus" &&
      aws_cloudwatch_event_target.job_failure["pipeline"].input_transformer[0].input_paths["status"] == "$.detail.currentPipelineExecutionStatus"
    )
    error_message = "Input paths must use the actual AWS event fields and may never repurpose an allowed metadata key to forward private diagnostics."
  }
}

run "delivery_permissions_and_retry_are_scoped" {
  command = plan
  variables {
    monitoring_enabled = true
    alert_email        = "operator@example.com"
  }
  assert {
    condition = (
      jsondecode(aws_iam_role.alert_delivery[0].assume_role_policy).Statement[0].Principal == { Service = "events.amazonaws.com" } &&
      jsondecode(aws_iam_role.alert_delivery[0].assume_role_policy).Statement[0].Action == "sts:AssumeRole" &&
      jsondecode(aws_iam_role.alert_delivery[0].assume_role_policy).Statement[0].Condition.StringEquals["aws:SourceAccount"] == "123456789012" &&
      toset(jsondecode(aws_iam_role.alert_delivery[0].assume_role_policy).Statement[0].Condition.ArnEquals["aws:SourceArn"]) == toset([
        for key in ["training", "processing", "publication", "transform", "pipeline"] :
        "arn:aws:events:ap-southeast-2:123456789012:rule/retail-forecast-${key}-failed"
      ]) &&
      jsondecode(aws_iam_role_policy.alert_delivery[0].policy).Statement == [
        { Effect = "Allow", Action = "sns:Publish", Resource = aws_sns_topic.operations[0].arn }
      ]
    )
    error_message = "Only the five exact EventBridge rules may assume a role that publishes to one topic."
  }
  assert {
    condition = (
      length(jsondecode(aws_sns_topic_policy.operations[0].policy).Statement) == 2 &&
      jsondecode(aws_sns_topic_policy.operations[0].policy).Statement[0].Principal == { Service = "cloudwatch.amazonaws.com" } &&
      jsondecode(aws_sns_topic_policy.operations[0].policy).Statement[0].Condition.StringEquals["aws:SourceAccount"] == "123456789012" &&
      toset(jsondecode(aws_sns_topic_policy.operations[0].policy).Statement[0].Condition.ArnEquals["aws:SourceArn"]) == toset([
        "arn:aws:cloudwatch:ap-southeast-2:123456789012:alarm:retail-forecast-api-errors",
        "arn:aws:cloudwatch:ap-southeast-2:123456789012:alarm:retail-forecast-ingest-errors",
        "arn:aws:cloudwatch:ap-southeast-2:123456789012:alarm:retail-forecast-alert-delivery-backlog"
      ]) &&
      jsondecode(aws_sns_topic_policy.operations[0].policy).Statement[1].Principal == { AWS = aws_iam_role.alert_delivery[0].arn } &&
      alltrue([for statement in jsondecode(aws_sns_topic_policy.operations[0].policy).Statement :
        statement.Action == "sns:Publish" && statement.Resource == aws_sns_topic.operations[0].arn
      ]) &&
      aws_cloudwatch_metric_alarm.api_errors.alarm_actions == toset([aws_sns_topic.operations[0].arn]) &&
      aws_cloudwatch_metric_alarm.ingest_errors.alarm_actions == toset([aws_sns_topic.operations[0].arn])
    )
    error_message = "Topic publication must be limited to the constrained role and exact CloudWatch alarms; both existing Lambda alarms must notify the topic."
  }
  assert {
    condition = (
      aws_sqs_queue.alert_delivery[0].sqs_managed_sse_enabled &&
      aws_sqs_queue.alert_delivery[0].message_retention_seconds == 604800 &&
      jsondecode(aws_sqs_queue_policy.alert_delivery[0].policy).Statement[0].Condition.StringEquals["aws:SourceAccount"] == "123456789012" &&
      toset(jsondecode(aws_sqs_queue_policy.alert_delivery[0].policy).Statement[0].Condition.ArnEquals["aws:SourceArn"]) == toset(local.failure_rule_arns) &&
      jsondecode(aws_sqs_queue_policy.alert_delivery[0].policy).Statement[0].Action == "sqs:SendMessage" &&
      alltrue([for target in aws_cloudwatch_event_target.job_failure :
        target.role_arn == aws_iam_role.alert_delivery[0].arn &&
        target.arn == aws_sns_topic.operations[0].arn &&
        target.dead_letter_config[0].arn == aws_sqs_queue.alert_delivery[0].arn &&
        target.retry_policy[0].maximum_event_age_in_seconds == 3600 &&
        target.retry_policy[0].maximum_retry_attempts == 10
      ]) &&
      aws_cloudwatch_metric_alarm.alert_delivery[0].metric_name == "ApproximateNumberOfMessagesVisible" &&
      aws_cloudwatch_metric_alarm.alert_delivery[0].threshold == 0 &&
      aws_cloudwatch_metric_alarm.alert_delivery[0].alarm_actions == toset([aws_sns_topic.operations[0].arn])
    )
    error_message = "Failed delivery must have bounded retries, an encrypted private queue, constrained sender and a backlog alarm."
  }
}

run "custom_budget_keeps_default_schedules_off" {
  command = plan
  variables {
    monitoring_enabled = true
    alert_email        = "operator@example.com"
    monthly_budget_usd = 25.50
  }
  assert {
    condition = (
      aws_budgets_budget.monthly[0].limit_amount == "25.5" &&
      aws_cloudwatch_event_rule.replay.state == "DISABLED" &&
      length(aws_instance.clearml) == 0 &&
      length(aws_sagemaker_pipeline.forecast) == 0
    )
    error_message = "Changing the budget must not turn on schedules, experiment servers or ML executions."
  }
}

run "missing_recipient_is_rejected" {
  command = plan
  variables {
    monitoring_enabled = true
  }
  expect_failures = [var.alert_email]
}

run "invalid_recipient_is_rejected" {
  command = plan
  variables {
    alert_email = "not-an-email"
  }
  expect_failures = [var.alert_email]
}

run "zero_budget_is_rejected" {
  command = plan
  variables {
    monthly_budget_usd = 0
  }
  expect_failures = [var.monthly_budget_usd]
}

run "oversized_budget_is_rejected" {
  command = plan
  variables {
    monthly_budget_usd = 1001
  }
  expect_failures = [var.monthly_budget_usd]
}

run "fractional_cents_are_rejected" {
  command = plan
  variables {
    monthly_budget_usd = 10.001
  }
  expect_failures = [var.monthly_budget_usd]
}
