resource "aws_cloudwatch_log_group" "functions" {
  for_each          = local.functions
  name              = "/aws/lambda/${local.prefix}-${each.key}"
  retention_in_days = 14
}
resource "aws_cloudwatch_log_group" "gateway" {
  name              = "/aws/apigateway/${local.prefix}"
  retention_in_days = 14
}
resource "aws_cloudwatch_log_group" "recovery" {
  name              = "/aws/vendedlogs/states/${local.prefix}-recovery"
  retention_in_days = 14
}
locals {
  background = {
    worker = {
      timeout = 30, memory = 256
    }
    outbox = {
      timeout = 60, memory = 256
    }
    drill = {
      timeout = 90, memory = 512
    }
  }
  base_environment = {
    ORDERFLOW_TABLE = aws_dynamodb_table.workspace.name
  }
}
resource "aws_lambda_function" "api" {
  function_name    = "${local.prefix}-api"
  role             = aws_iam_role.functions["api"].arn
  runtime          = "python3.12"
  handler          = "aws.handlers.api.handler"
  filename         = var.lambda_zip
  source_code_hash = filebase64sha256(var.lambda_zip)
  timeout          = 20
  memory_size      = 256
  environment {
    variables = merge(local.base_environment, {
      AUTH_ENABLED      = "true"
      AUTH_ISSUER       = local.auth_issuer
      AUTH_CLIENT_ID    = aws_cognito_user_pool_client.web.id
      AUTH_DOMAIN       = local.auth_domain
      AUTH_CALLBACK_URL = local.auth_callback_url
      AUTH_LOGOUT_URL   = local.auth_callback_url
      AUTH_SCOPES       = join(" ", local.auth_scopes)
      DRILL_MACHINE_ARN = "arn:${local.partition}:states:${var.region}:${local.account}:stateMachine:${local.prefix}-recovery"
    })
  }
  depends_on = [aws_iam_role_policy.functions, aws_cloudwatch_log_group.functions]
}
resource "aws_lambda_function" "background" {
  for_each         = local.background
  function_name    = "${local.prefix}-${each.key}"
  role             = aws_iam_role.functions[each.key].arn
  runtime          = "python3.12"
  handler          = "aws.handlers.${each.key}.handler"
  filename         = var.lambda_zip
  source_code_hash = filebase64sha256(var.lambda_zip)
  timeout          = each.value.timeout
  memory_size      = each.value.memory
  environment {
    variables = merge(local.base_environment, each.key == "outbox" ? {
      WORK_QUEUE_URL = aws_sqs_queue.work.id
      } : {
    })
  }
  depends_on = [aws_iam_role_policy.functions, aws_cloudwatch_log_group.functions]
}
resource "aws_lambda_event_source_mapping" "work" {
  event_source_arn        = aws_sqs_queue.work.arn
  function_name           = aws_lambda_function.background["worker"].arn
  enabled                 = var.worker_enabled
  batch_size              = 5
  function_response_types = ["ReportBatchItemFailures"]
  scaling_config {
    maximum_concurrency = 2
  }
  depends_on = [aws_iam_role_policy.functions]
}
resource "aws_lambda_event_source_mapping" "outbox" {
  event_source_arn                   = aws_dynamodb_table.workspace.stream_arn
  function_name                      = aws_lambda_function.background["outbox"].arn
  enabled                            = var.outbox_enabled
  starting_position                  = "TRIM_HORIZON"
  batch_size                         = 25
  maximum_batching_window_in_seconds = 1
  parallelization_factor             = 1
  maximum_retry_attempts             = 3
  maximum_record_age_in_seconds      = 3600
  bisect_batch_on_function_error     = true
  function_response_types            = ["ReportBatchItemFailures"]
  destination_config {
    on_failure {
      destination_arn = aws_sqs_queue.stream_failures.arn
    }
  }
  filter_criteria {
    filter {
      pattern = jsonencode({
        eventName = ["INSERT", "MODIFY"], dynamodb = {
          NewImage = {
            SK = {
              S = [{
                prefix = "outbox#"
              }]
              }, payload = {
              M = {
                status = {
                  S = ["pending"]
                }
              }
            }
          }
        }
      })
    }
  }
  depends_on = [aws_iam_role_policy.functions]
}
resource "aws_sfn_state_machine" "recovery" {
  name     = "${local.prefix}-recovery"
  role_arn = aws_iam_role.recovery.arn
  type     = "STANDARD"
  logging_configuration {
    log_destination        = "${aws_cloudwatch_log_group.recovery.arn}:*"
    include_execution_data = false
    level                  = "ERROR"
  }
  definition = jsonencode({
    Comment        = "Isolated synthetic recovery drill; only evidence is saved to the live workspace"
    StartAt        = "RunIsolatedDrill"
    TimeoutSeconds = 180
    States = {
      RunIsolatedDrill = {
        Type = "Task", Resource = "arn:${local.partition}:states:::lambda:invoke", Parameters = {
          FunctionName = aws_lambda_function.background["drill"].arn, "Payload.$" = "$"
        }, ResultPath  = "$.result", Next = "CheckEvidence",
        Catch          = [{ ErrorEquals = ["States.ALL"], ResultPath = "$.failure", Next = "RecordFailure" }]
      }
      RecordFailure = {
        Type = "Task", Resource = "arn:${local.partition}:states:::lambda:invoke", Parameters = {
          FunctionName = aws_lambda_function.background["drill"].arn, "Payload.$" = "$"
        }, Next        = "EvidenceFailed"
      }
      CheckEvidence = {
        Type = "Choice", Choices = [{
          Variable  = "$.result.Payload.passed", BooleanEquals = true, Next = "Verified"
        }], Default = "EvidenceFailed"
      }
      Verified = {
        Type = "Succeed"
      }
      EvidenceFailed = {
        Type = "Fail", Error = "RecoveryEvidenceFailed", Cause = "The isolated drill did not pass every invariant"
      }
    }
  })
  depends_on = [aws_iam_role_policy.recovery]
}
resource "aws_cloudwatch_event_rule" "repair" {
  name                = "${local.prefix}-outbox-repair"
  schedule_expression = var.schedule_expression
  state               = var.schedule_enabled ? "ENABLED" : "DISABLED"
}
resource "aws_cloudwatch_event_target" "repair" {
  rule = aws_cloudwatch_event_rule.repair.name
  arn  = aws_lambda_function.background["outbox"].arn
  retry_policy {
    maximum_event_age_in_seconds = 3600
    maximum_retry_attempts       = 2
  }
}
resource "aws_lambda_permission" "repair" {
  statement_id  = "AllowOutboxRepair"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.background["outbox"].function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.repair.arn
}
resource "aws_apigatewayv2_api" "api" {
  name          = local.prefix
  protocol_type = "HTTP"
}
resource "aws_apigatewayv2_integration" "api" {
  api_id                 = aws_apigatewayv2_api.api.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.api.invoke_arn
  payload_format_version = "2.0"
}
resource "aws_apigatewayv2_route" "api" {
  api_id               = aws_apigatewayv2_api.api.id
  route_key            = "ANY /api/{proxy+}"
  target               = "integrations/${aws_apigatewayv2_integration.api.id}"
  authorization_type   = "JWT"
  authorizer_id        = aws_apigatewayv2_authorizer.app.id
  authorization_scopes = ["orderflow/read"]
}
resource "aws_apigatewayv2_stage" "api" {
  api_id      = aws_apigatewayv2_api.api.id
  name        = "$default"
  auto_deploy = true
  default_route_settings {
    throttling_burst_limit = var.api_enabled ? 10 : 0
    throttling_rate_limit  = var.api_enabled ? 5 : 0
  }
  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.gateway.arn
    format = jsonencode({
      requestId = "$context.requestId", routeKey = "$context.routeKey", status = "$context.status", responseLength = "$context.responseLength"
    })
  }
}
resource "aws_lambda_permission" "api" {
  statement_id  = "AllowGateway"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.api.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.api.execution_arn}/*/*/api/*"
}
data "aws_cloudfront_cache_policy" "static" {
  name = "Managed-CachingOptimized"
}
data "aws_cloudfront_cache_policy" "api" {
  name = "Managed-CachingDisabled"
}
data "aws_cloudfront_origin_request_policy" "api" {
  name = "Managed-AllViewerExceptHostHeader"
}
data "aws_cloudfront_response_headers_policy" "security" {
  name = "Managed-SecurityHeadersPolicy"
}
resource "aws_cloudfront_origin_access_control" "web" {
  name                              = local.prefix
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}
resource "aws_cloudfront_distribution" "app" {
  enabled             = true
  default_root_object = "index.html"
  price_class         = "PriceClass_All"
  origin {
    origin_id                = "web"
    domain_name              = aws_s3_bucket.app["frontend"].bucket_regional_domain_name
    origin_access_control_id = aws_cloudfront_origin_access_control.web.id
  }
  origin {
    origin_id   = "api"
    domain_name = replace(aws_apigatewayv2_api.api.api_endpoint, "https://", "")
    custom_origin_config {
      http_port              = 80
      https_port             = 443
      origin_protocol_policy = "https-only"
      origin_ssl_protocols   = ["TLSv1.2"]
    }
  }
  default_cache_behavior {
    target_origin_id           = "web"
    allowed_methods            = ["GET", "HEAD", "OPTIONS"]
    cached_methods             = ["GET", "HEAD"]
    viewer_protocol_policy     = "redirect-to-https"
    cache_policy_id            = data.aws_cloudfront_cache_policy.static.id
    response_headers_policy_id = data.aws_cloudfront_response_headers_policy.security.id
    compress                   = true
  }
  ordered_cache_behavior {
    path_pattern               = "/api/*"
    target_origin_id           = "api"
    allowed_methods            = ["DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"]
    cached_methods             = ["GET", "HEAD"]
    viewer_protocol_policy     = "https-only"
    cache_policy_id            = data.aws_cloudfront_cache_policy.api.id
    origin_request_policy_id   = data.aws_cloudfront_origin_request_policy.api.id
    response_headers_policy_id = data.aws_cloudfront_response_headers_policy.security.id
  }
  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }
  viewer_certificate {
    cloudfront_default_certificate = true
  }
}
