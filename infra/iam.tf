resource "aws_iam_role" "functions" {
  for_each = local.functions
  name     = "${local.prefix}-${each.key}"
  assume_role_policy = jsonencode({
    Version = "2012-10-17", Statement = [{
      Effect = "Allow", Principal = {
        Service = "lambda.amazonaws.com"
      }, Action = "sts:AssumeRole"
    }]
  })
}
resource "aws_iam_role_policy" "functions" {
  for_each = local.functions
  role     = aws_iam_role.functions[each.key].id
  policy = jsonencode({
    Version = "2012-10-17", Statement = concat([
      {
        Effect = "Allow", Action = local.table_actions, Resource = [aws_dynamodb_table.workspace.arn], Condition = { "ForAllValues:StringEquals" = { "dynamodb:LeadingKeys" = ["WORKSPACE"] } }
      },
      {
        Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents"], Resource = ["${aws_cloudwatch_log_group.functions[each.key].arn}:*"]
      }
      ], each.key == "worker" ? [
      {
        Effect = "Allow", Action = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"], Resource = [aws_sqs_queue.work.arn]
      }
      ] : [], each.key == "outbox" ? [
      {
        Effect = "Allow", Action = ["sqs:SendMessage"], Resource = [aws_sqs_queue.work.arn, aws_sqs_queue.stream_failures.arn]
      },
      {
        Effect = "Allow", Action = ["dynamodb:DescribeStream", "dynamodb:GetRecords", "dynamodb:GetShardIterator"], Resource = ["${aws_dynamodb_table.workspace.arn}/stream/*"]
      },
      {
        Effect = "Allow", Action = ["dynamodb:ListStreams"], Resource = ["*"]
      }
      ] : [], each.key == "api" ? [
      {
        Effect = "Allow", Action = ["states:StartExecution"], Resource = ["arn:${local.partition}:states:${var.region}:${local.account}:stateMachine:${local.prefix}-recovery"]
      }
    ] : [])
  })
}
resource "aws_iam_role" "recovery" {
  name = "${local.prefix}-recovery"
  assume_role_policy = jsonencode({
    Version = "2012-10-17", Statement = [{
      Effect = "Allow", Principal = {
        Service = "states.amazonaws.com"
      }, Action = "sts:AssumeRole"
    }]
  })
}
resource "aws_iam_role_policy" "recovery" {
  role = aws_iam_role.recovery.id
  policy = jsonencode({
    Version = "2012-10-17", Statement = [
      {
        Effect = "Allow", Action = ["lambda:InvokeFunction"], Resource = aws_lambda_function.background["drill"].arn
      },
      {
        Effect = "Allow", Action = ["logs:CreateLogDelivery", "logs:GetLogDelivery", "logs:UpdateLogDelivery", "logs:DeleteLogDelivery", "logs:ListLogDeliveries", "logs:PutResourcePolicy", "logs:DescribeResourcePolicies", "logs:DescribeLogGroups"], Resource = "*"
      }
    ]
  })
}
