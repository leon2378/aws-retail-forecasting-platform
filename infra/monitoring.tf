# All notification resources are opt-in. These alerts never stop compute or cap
# spending, and the account-wide budget intentionally excludes credits/refunds.
locals {
  failure_alerts = {
    training = {
      title        = "Training job failed"
      detail_type  = "SageMaker Training Job State Change"
      name_field   = "TrainingJobName"
      status_field = "TrainingJobStatus"
      prefixes     = ["${local.prefix}-train-"]
    }
    processing = {
      title        = "Processing job failed"
      detail_type  = "SageMaker Processing Job State Change"
      name_field   = "ProcessingJobName"
      status_field = "ProcessingJobStatus"
      prefixes     = ["${local.prefix}-prepare-", "${local.prefix}-evaluate-", "${local.prefix}-reject-"]
    }
    publication = {
      title        = "Forecast publication failed"
      detail_type  = "SageMaker Processing Job State Change"
      name_field   = "ProcessingJobName"
      status_field = "ProcessingJobStatus"
      prefixes     = ["${local.prefix}-publish-"]
    }
    transform = {
      title        = "Batch forecast job failed"
      detail_type  = "SageMaker Transform Job State Change"
      name_field   = "TransformJobName"
      status_field = "TransformJobStatus"
      prefixes     = ["${local.prefix}-forecast-"]
    }
    pipeline = {
      title        = "Forecast pipeline failed"
      detail_type  = "SageMaker Model Building Pipeline Execution Status Change"
      name_field   = "pipelineArn"
      status_field = "currentPipelineExecutionStatus"
      prefixes     = []
    }
  }
  enabled_failure_alerts = var.monitoring_enabled ? local.failure_alerts : {}
  failure_rule_arns = [for key in keys(local.failure_alerts) :
    "${local.arn_prefix}:events:${var.region}:${local.account}:rule/${local.prefix}-${key}-failed"
  ]
  notification_alarm_arns = [for suffix in ["api-errors", "ingest-errors", "alert-delivery-backlog"] :
    "${local.arn_prefix}:cloudwatch:${var.region}:${local.account}:alarm:${local.prefix}-${suffix}"
  ]
}

resource "aws_budgets_budget" "monthly" {
  count        = var.monitoring_enabled ? 1 : 0
  account_id   = local.account
  name         = "${local.prefix}-account-monthly-gross"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"
  # No project or region filter: this budget covers the entire account.
  cost_types {
    include_credit             = false
    include_refund             = false
    include_discount           = false
    include_other_subscription = true
    include_recurring          = true
    include_subscription       = true
    include_support            = true
    include_tax                = true
    include_upfront            = true
    use_amortized              = false
    use_blended                = false
  }
  dynamic "notification" {
    for_each = [50, 80, 100]
    content {
      comparison_operator        = "GREATER_THAN"
      threshold                  = notification.value
      threshold_type             = "PERCENTAGE"
      notification_type          = "ACTUAL"
      subscriber_email_addresses = [var.alert_email]
    }
  }
  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.alert_email]
  }
}

resource "aws_sns_topic" "operations" {
  count        = var.monitoring_enabled ? 1 : 0
  name         = "${local.prefix}-operations-alerts"
  display_name = "SupplySight operations"
}

resource "aws_sns_topic_subscription" "operations_email" {
  count                  = var.monitoring_enabled ? 1 : 0
  topic_arn              = aws_sns_topic.operations[0].arn
  protocol               = "email"
  endpoint               = var.alert_email
  endpoint_auto_confirms = false
}

# EventBridge supports a target execution role for SNS. Restrict its trust to
# this account's exact rules, rather than relying on unsupported conditions in
# a direct EventBridge service-principal SNS topic statement.
resource "aws_iam_role" "alert_delivery" {
  count = var.monitoring_enabled ? 1 : 0
  name  = "${local.prefix}-alert-delivery"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "events.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = {
        StringEquals = { "aws:SourceAccount" = local.account }
        ArnEquals    = { "aws:SourceArn" = local.failure_rule_arns }
      }
    }]
  })
}

resource "aws_iam_role_policy" "alert_delivery" {
  count = var.monitoring_enabled ? 1 : 0
  name  = "PublishOnlyOperationsTopic"
  role  = aws_iam_role.alert_delivery[0].id
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = "sns:Publish", Resource = aws_sns_topic.operations[0].arn }]
  })
}

resource "aws_sns_topic_policy" "operations" {
  count = var.monitoring_enabled ? 1 : 0
  arn   = aws_sns_topic.operations[0].arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "ConfiguredAlarmsOnly", Effect = "Allow",
        Principal = { Service = "cloudwatch.amazonaws.com" },
        Action    = "sns:Publish", Resource = aws_sns_topic.operations[0].arn,
        Condition = {
          StringEquals = { "aws:SourceAccount" = local.account }
          ArnEquals    = { "aws:SourceArn" = local.notification_alarm_arns }
        }
      },
      {
        Sid       = "ScopedEventDeliveryRole", Effect = "Allow",
        Principal = { AWS = aws_iam_role.alert_delivery[0].arn },
        Action    = "sns:Publish", Resource = aws_sns_topic.operations[0].arn
      }
    ]
  })
}

resource "aws_cloudwatch_event_rule" "job_failure" {
  for_each    = local.enabled_failure_alerts
  name        = "${local.prefix}-${each.key}-failed"
  description = "${each.value.title}; this project's exact stage namespace only."
  state       = "ENABLED"
  event_pattern = jsonencode({
    source        = ["aws.sagemaker"]
    account       = [local.account]
    region        = [var.region]
    "detail-type" = [each.value.detail_type]
    detail = merge(
      { (each.value.status_field) = ["Failed"] },
      jsondecode(each.key == "pipeline" ? jsonencode({ pipelineArn = [local.pipeline_arn] }) :
      jsonencode({ (each.value.name_field) = [for prefix in each.value.prefixes : { prefix = prefix }] }))
    )
  })
}

# The queue retains original undelivered events for operator investigation. It
# is private and encrypted with SQS-managed encryption, with a seven-day TTL.
resource "aws_sqs_queue" "alert_delivery" {
  count                     = var.monitoring_enabled ? 1 : 0
  name                      = "${local.prefix}-alert-delivery-dlq"
  message_retention_seconds = 604800
  sqs_managed_sse_enabled   = true
}

resource "aws_sqs_queue_policy" "alert_delivery" {
  count     = var.monitoring_enabled ? 1 : 0
  queue_url = aws_sqs_queue.alert_delivery[0].url
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow", Principal = { Service = "events.amazonaws.com" },
      Action = "sqs:SendMessage", Resource = aws_sqs_queue.alert_delivery[0].arn,
      Condition = {
        StringEquals = { "aws:SourceAccount" = local.account }
        ArnEquals    = { "aws:SourceArn" = local.failure_rule_arns }
      }
    }]
  })
}

resource "aws_cloudwatch_event_target" "job_failure" {
  for_each  = local.enabled_failure_alerts
  rule      = aws_cloudwatch_event_rule.job_failure[each.key].name
  target_id = "operations-email"
  arn       = aws_sns_topic.operations[0].arn
  role_arn  = aws_iam_role.alert_delivery[0].arn
  retry_policy {
    maximum_event_age_in_seconds = 3600
    maximum_retry_attempts       = 10
  }
  dead_letter_config {
    arn = aws_sqs_queue.alert_delivery[0].arn
  }
  # Never email FailureReason, role ARN, hyperparameters, environment, inputs,
  # customer data or the full event. Detailed diagnostics remain in AWS.
  input_transformer {
    input_paths = merge({
      event_id = "$.id", time = "$.time", region = "$.region",
      status   = "$.detail.${each.value.status_field}"
    }, each.key == "pipeline" ? { execution = "$.detail.pipelineExecutionArn" } : { job = "$.detail.${each.value.name_field}" })
    input_template = jsonencode(merge({
      project = local.prefix,
      alert   = each.value.title,
      job     = each.key == "pipeline" ? local.prefix : "<job>",
      status  = "<status>", time = "<time>", region = "<region>", event_id = "<event_id>",
      action  = "Inspect the named run in the AWS console and follow docs/costs-and-alerts.md. Alerts do not stop jobs."
    }, each.key == "pipeline" ? { execution = "<execution>" } : {}))
  }
  depends_on = [aws_iam_role_policy.alert_delivery, aws_sns_topic_policy.operations, aws_sqs_queue_policy.alert_delivery]
}

resource "aws_cloudwatch_metric_alarm" "alert_delivery" {
  count               = var.monitoring_enabled ? 1 : 0
  alarm_name          = "${local.prefix}-alert-delivery-backlog"
  alarm_description   = "An operations alert entered the EventBridge delivery DLQ. Check the private queue and topic permissions."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = { QueueName = aws_sqs_queue.alert_delivery[0].name }
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.operations[0].arn]
}
