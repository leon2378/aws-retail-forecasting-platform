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
