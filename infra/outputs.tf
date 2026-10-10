output "website_url" {
  value = "https://${aws_cloudfront_distribution.app.domain_name}"
}
output "api_url" {
  value = aws_apigatewayv2_api.api.api_endpoint
}
output "workspace_table" {
  value = aws_dynamodb_table.workspace.name
}
output "work_queue_url" {
  value = aws_sqs_queue.work.id
}
output "dead_letter_queue_url" {
  value = aws_sqs_queue.dead_letter.id
}
output "outbox_failure_queue_url" {
  value = aws_sqs_queue.stream_failures.id
}
output "worker_mapping_uuid" {
  value = aws_lambda_event_source_mapping.work.uuid
}
output "outbox_mapping_uuid" {
  value = aws_lambda_event_source_mapping.outbox.uuid
}
output "recovery_machine_arn" {
  value = aws_sfn_state_machine.recovery.arn
}
output "user_pool_id" {
  value = aws_cognito_user_pool.app.id
}
output "user_pool_client_id" {
  value = aws_cognito_user_pool_client.web.id
}
output "frontend_bucket" {
  value = aws_s3_bucket.app["frontend"].id
}
output "delivery_bucket" {
  value = aws_s3_bucket.app["delivery"].id
}
output "distribution_id" {
  value = aws_cloudfront_distribution.app.id
}
output "operations_dashboard_url" {
  value = "https://${var.region}.console.aws.amazon.com/cloudwatch/home?region=${var.region}#dashboards:name=${aws_cloudwatch_dashboard.operations.dashboard_name}"
}
output "notification_topic_arn" {
  value = var.monitoring_enabled ? aws_sns_topic.operations[0].arn : null
}
output "delivery_pipeline_name" {
  value = var.enable_delivery ? aws_codepipeline.app[0].name : null
}
