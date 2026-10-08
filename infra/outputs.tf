output "frontend_url" {
  value = "https://${aws_cloudfront_distribution.app.domain_name}"
}
output "api_url" {
  value = aws_apigatewayv2_api.api.api_endpoint
}
output "data_bucket" {
  value = aws_s3_bucket.app["data"].id
}
output "frontend_bucket" {
  value = aws_s3_bucket.app["frontend"].id
}
output "delivery_bucket" {
  value = aws_s3_bucket.app["delivery"].id
}
output "ecr_repository_url" {
  value = aws_ecr_repository.model.repository_url
}
output "sagemaker_role_arn" {
  value = aws_iam_role.sagemaker.arn
}
output "results_table" {
  value = aws_dynamodb_table.results.name
}
output "pipeline_name" {
  value = local.prefix
}
output "distribution_id" {
  value = aws_cloudfront_distribution.app.id
}
output "ingest_function_name" {
  value = aws_lambda_function.ingest.function_name
}
output "clearml_instance_id" {
  value = try(aws_instance.clearml[0].id, null)
}
output "cognito_user_pool_id" {
  description = "Pool for IAM-authorized user invitations and viewer/planner group assignment."
  value       = aws_cognito_user_pool.app.id
}
output "auth_public_settings" {
  description = "Public sign-in settings; this application client has no secret."
  value = {
    issuer       = local.auth_issuer
    client_id    = aws_cognito_user_pool_client.web.id
    domain       = local.auth_domain
    callback_url = local.auth_callback_url
    logout_url   = local.auth_logout_url
    scopes       = local.auth_scopes
  }
}
