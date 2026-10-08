resource "aws_cloudwatch_log_group" "api" {

  name              = "/aws/lambda/${local.prefix}-api"
  retention_in_days = 30

}
resource "aws_cloudwatch_log_group" "ingest" {

  name              = "/aws/lambda/${local.prefix}-ingest"
  retention_in_days = 30

}
resource "aws_cloudwatch_log_group" "gateway" {

  name              = "/aws/apigateway/${local.prefix}"
  retention_in_days = 30

}

resource "aws_lambda_function" "api" {

  function_name    = "${local.prefix}-api"
  role             = aws_iam_role.api.arn
  runtime          = "python3.11"
  handler          = "aws.handlers.api.handler"
  filename         = var.lambda_zip
  source_code_hash = filebase64sha256(var.lambda_zip)
  timeout          = 20
  memory_size      = 256
  environment {
    variables = {
      RESULTS_TABLE     = aws_dynamodb_table.results.name
      DATA_BUCKET       = aws_s3_bucket.app["data"].id
      PIPELINE_NAME     = local.prefix
      AUTH_ENABLED      = "true"
      AUTH_ISSUER       = local.auth_issuer
      AUTH_CLIENT_ID    = aws_cognito_user_pool_client.web.id
      AUTH_DOMAIN       = local.auth_domain
      AUTH_CALLBACK_URL = local.auth_callback_url
      AUTH_LOGOUT_URL   = local.auth_logout_url
      AUTH_SCOPES       = join(" ", local.auth_scopes)
    }
  }
  depends_on = [aws_cloudwatch_log_group.api, aws_iam_role_policy.api]

}
resource "aws_lambda_function" "ingest" {

  function_name    = "${local.prefix}-ingest"
  role             = aws_iam_role.ingest.arn
  runtime          = "python3.11"
  handler          = "aws.handlers.ingest.handler"
  filename         = var.lambda_zip
  source_code_hash = filebase64sha256(var.lambda_zip)
  timeout          = 120
  memory_size      = 512
  environment {
    variables = { RESULTS_TABLE = aws_dynamodb_table.results.name, DATA_BUCKET = aws_s3_bucket.app["data"].id,
    PIPELINE_NAME = local.prefix, INITIAL_CUTOFF = tostring(var.initial_cutoff), CANDIDATE_MODEL = var.candidate_model }
  }
  depends_on = [aws_cloudwatch_log_group.ingest, aws_iam_role_policy.ingest]

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
  authorization_scopes = ["retail/read"]

}
resource "aws_apigatewayv2_stage" "api" {

  api_id      = aws_apigatewayv2_api.api.id
  name        = "$default"
  auto_deploy = true
  default_route_settings {
    throttling_burst_limit = 10
    throttling_rate_limit  = 5

  }
  access_log_settings {

    destination_arn = aws_cloudwatch_log_group.gateway.arn
    format          = jsonencode({ requestId = "$context.requestId", routeKey = "$context.routeKey", status = "$context.status", responseLength = "$context.responseLength" })

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

resource "aws_cloudwatch_event_rule" "replay" {

  name                = "${local.prefix}-daily-replay"
  schedule_expression = var.schedule_expression
  state               = var.schedule_enabled ? "ENABLED" : "DISABLED"

}
resource "aws_cloudwatch_event_target" "replay" {

  rule = aws_cloudwatch_event_rule.replay.name
  arn  = aws_lambda_function.ingest.arn
  retry_policy {
    maximum_event_age_in_seconds = 3600
    maximum_retry_attempts       = 2

  }

}
resource "aws_lambda_permission" "replay" {

  statement_id  = "AllowScheduledReplay"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.ingest.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.replay.arn

}
resource "aws_sagemaker_model_package_group" "forecast" {

  model_package_group_name        = local.prefix
  model_package_group_description = "Chronological retail forecasts with explicit release checks"

}
resource "aws_sagemaker_pipeline" "forecast" {

  count                 = var.pipeline_definition_file != "" ? 1 : 0
  pipeline_name         = local.prefix
  pipeline_display_name = local.prefix
  role_arn              = aws_iam_role.sagemaker.arn
  pipeline_definition   = file(var.pipeline_definition_file)
  # The first definition bootstraps the resource; application deliveries own
  # subsequent image/definition updates through the deploy buildspec.
  lifecycle {
    ignore_changes = [pipeline_definition]
  }
  parallelism_configuration {
    max_parallel_execution_steps = 1
  }
  depends_on = [aws_iam_role_policy.sagemaker]

}

resource "aws_cloudwatch_metric_alarm" "api_errors" {

  alarm_name          = "${local.prefix}-api-errors"
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = aws_lambda_function.api.function_name }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

}
resource "aws_cloudwatch_metric_alarm" "ingest_errors" {

  alarm_name          = "${local.prefix}-ingest-errors"
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = aws_lambda_function.ingest.function_name }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

}
