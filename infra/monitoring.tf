resource "aws_sns_topic" "operations" {
  count = var.monitoring_enabled ? 1 : 0
  name  = "${local.prefix}-operations"
}
resource "aws_sns_topic_subscription" "email" {
  count     = var.monitoring_enabled ? 1 : 0
  topic_arn = aws_sns_topic.operations[0].arn
  protocol  = "email"
  endpoint  = var.alert_email
}
resource "aws_sns_topic_policy" "operations" {
  count = var.monitoring_enabled ? 1 : 0
  arn   = aws_sns_topic.operations[0].arn
  policy = jsonencode({
    Version = "2012-10-17", Statement = [
      {
        Sid = "AccountOwner", Effect = "Allow", Principal = {
          AWS = "arn:${local.partition}:iam::${local.account}:root"
          }, Action = [
          "sns:GetTopicAttributes", "sns:SetTopicAttributes", "sns:AddPermission",
          "sns:RemovePermission", "sns:DeleteTopic", "sns:Subscribe",
          "sns:ListSubscriptionsByTopic", "sns:Publish"
        ], Resource = aws_sns_topic.operations[0].arn
      },
      {
        Sid = "CloudWatchAlarms", Effect = "Allow", Principal = {
          Service = "cloudwatch.amazonaws.com"
          }, Action = "sns:Publish", Resource = aws_sns_topic.operations[0].arn, Condition = {
          StringEquals = {
            "aws:SourceAccount" = local.account
            }, ArnLike = {
            "aws:SourceArn" = "arn:${local.partition}:cloudwatch:${var.region}:${local.account}:alarm:${local.prefix}-*"
          }
        }
      }
    ]
  })
}
locals {
  alarm_actions = var.monitoring_enabled ? [aws_sns_topic.operations[0].arn] : []
}
resource "aws_cloudwatch_metric_alarm" "function_errors" {
  for_each    = local.functions
  alarm_name  = "${local.prefix}-${each.key}-errors"
  namespace   = "AWS/Lambda"
  metric_name = "Errors"
  dimensions = {
    FunctionName = "${local.prefix}-${each.key}"
  }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions
}
resource "aws_cloudwatch_metric_alarm" "dlq" {
  for_each = {
    work = aws_sqs_queue.dead_letter.name, outbox = aws_sqs_queue.stream_failures.name
  }
  alarm_name  = "${local.prefix}-${each.key}-dead-letters"
  namespace   = "AWS/SQS"
  metric_name = "ApproximateNumberOfMessagesVisible"
  dimensions = {
    QueueName = each.value
  }
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions
}
resource "aws_cloudwatch_metric_alarm" "queue_age" {
  alarm_name  = "${local.prefix}-queue-age"
  namespace   = "AWS/SQS"
  metric_name = "ApproximateAgeOfOldestMessage"
  dimensions = {
    QueueName = aws_sqs_queue.work.name
  }
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 2
  threshold           = 600
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions
}
resource "aws_cloudwatch_metric_alarm" "recovery" {
  alarm_name  = "${local.prefix}-recovery-failed"
  namespace   = "AWS/States"
  metric_name = "ExecutionsFailed"
  dimensions = {
    StateMachineArn = aws_sfn_state_machine.recovery.arn
  }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions
}
resource "aws_cloudwatch_log_metric_filter" "publisher_failure" {
  name           = "${local.prefix}-publisher-failure"
  log_group_name = aws_cloudwatch_log_group.functions["outbox"].name
  pattern        = "outbox_publication_failed"
  metric_transformation {
    name          = "PublicationFailures"
    namespace     = "OrderFlow/${local.prefix}"
    value         = "1"
    default_value = "0"
  }
}
resource "aws_cloudwatch_metric_alarm" "publication" {
  alarm_name          = "${local.prefix}-publication-failed"
  namespace           = "OrderFlow/${local.prefix}"
  metric_name         = "PublicationFailures"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions
}
resource "aws_cloudwatch_metric_alarm" "api_failures" {
  alarm_name          = "${local.prefix}-api-5xx"
  namespace           = "AWS/ApiGateway"
  metric_name         = "5xx"
  dimensions          = { ApiId = aws_apigatewayv2_api.api.id }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions
}
resource "aws_cloudwatch_dashboard" "operations" {
  dashboard_name = "${local.prefix}-operations"
  dashboard_body = jsonencode({
    widgets = [
      {
        type = "text", x = 0, y = 0, width = 24, height = 2, properties = {
          markdown = "# OrderFlow operations\nSynthetic catalog; payments and shipping are simulations. Queue alarms show delivery health. Email alerts require opt-in and SNS confirmation."
        }
      },
      {
        type = "metric", x = 0, y = 2, width = 12, height = 6, properties = {
          title = "Function errors", region = var.region, view = "timeSeries", stat = "Sum", period = 300, metrics = [for name in local.functions : ["AWS/Lambda", "Errors", "FunctionName", "${local.prefix}-${name}"]]
        }
      },
      {
        type = "metric", x = 12, y = 2, width = 12, height = 6, properties = {
          title = "Queue delivery", region = var.region, view = "timeSeries", period = 300, metrics = [
            ["AWS/SQS", "ApproximateNumberOfMessagesVisible", "QueueName", aws_sqs_queue.work.name, {
              stat = "Maximum"
            }],
            ["AWS/SQS", "ApproximateNumberOfMessagesVisible", "QueueName", aws_sqs_queue.dead_letter.name, {
              stat = "Maximum"
            }],
            ["AWS/SQS", "ApproximateNumberOfMessagesVisible", "QueueName", aws_sqs_queue.stream_failures.name, {
              stat = "Maximum"
            }]
          ]
        }
      },
      {
        type = "metric", x = 0, y = 8, width = 12, height = 6, properties = {
          title = "API response time and errors", region = var.region, view = "timeSeries", period = 300, metrics = [
            ["AWS/ApiGateway", "Latency", "ApiId", aws_apigatewayv2_api.api.id, {
              stat = "p95"
            }],
            ["AWS/ApiGateway", "5xx", "ApiId", aws_apigatewayv2_api.api.id, {
              stat = "Sum", yAxis = "right"
            }]
          ]
        }
      },
      {
        type = "metric", x = 12, y = 8, width = 12, height = 6, properties = {
          title = "Recovery workflow and publication failures", region = var.region, view = "timeSeries", stat = "Sum", period = 300, metrics = [
            ["AWS/States", "ExecutionsFailed", "StateMachineArn", aws_sfn_state_machine.recovery.arn],
            ["OrderFlow/${local.prefix}", "PublicationFailures"]
          ]
        }
      }
    ]
  })
}
resource "aws_budgets_budget" "monthly" {
  count        = var.monitoring_enabled ? 1 : 0
  name         = "${local.prefix}-monthly-account-spend"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"
  cost_types {
    include_credit   = false
    include_refund   = false
    include_discount = false
    include_tax      = true
    include_support  = true
    use_blended      = false
    use_amortized    = false
  }
  dynamic "notification" {
    for_each = [{
      type = "ACTUAL", threshold = 50
      }, {
      type = "ACTUAL", threshold = 100
      }, {
      type = "FORECASTED", threshold = 100
    }]
    content {
      comparison_operator        = "GREATER_THAN"
      threshold                  = notification.value.threshold
      threshold_type             = "PERCENTAGE"
      notification_type          = notification.value.type
      subscriber_email_addresses = [var.alert_email]
    }
  }
}
