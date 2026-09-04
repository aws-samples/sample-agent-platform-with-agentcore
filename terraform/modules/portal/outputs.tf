output "portal_url" {
  value = "https://${aws_cloudfront_distribution.portal.domain_name}"
}

output "distribution_id" {
  value = aws_cloudfront_distribution.portal.id
}

output "frontend_bucket_name" {
  value = aws_s3_bucket.frontend.bucket
}

output "alb_dns_name" {
  value = aws_lb.portal.dns_name
}

output "user_pool_id" {
  value = aws_cognito_user_pool.portal.id
}

output "user_pool_client_id" {
  value = aws_cognito_user_pool_client.portal.id
}

output "schedule_runner_function" {
  value = aws_lambda_function.schedule_runner.function_name
}

output "schedule_dlq_url" {
  value = aws_sqs_queue.schedule_dlq.url
}

# The operator creates this secret by hand (see docs/deployment.md); the name
# is suffix-dependent, so read it from here rather than typing it.
output "portal_admin_secret_name" {
  value = local.portal_admin_secret
}

output "service_entry_api_url" {
  value = "https://${aws_api_gateway_rest_api.service_entry.id}.execute-api.${local.region}.amazonaws.com/svc/"
}

output "service_entry_api_id" {
  value = aws_api_gateway_rest_api.service_entry.id
}

output "service_entry_api_execution_arn" {
  value = aws_api_gateway_rest_api.service_entry.execution_arn
}
