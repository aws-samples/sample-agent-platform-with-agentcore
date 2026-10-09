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
  description = "Fixed name of the schedule-runner Lambda (the function itself is a workload)."
  value       = local.schedule_runner_function_name
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

output "namespace" {
  value = local.namespace
}

# ---- what the workloads layer needs to run the backend here (no secrets) ----
output "workload_facts" {
  description = "Published to the foundation facts parameter for terraform/workloads/modules/portal."
  value = {
    namespace                 = local.namespace
    backend_role_arn          = aws_iam_role.backend.arn
    service_security_group_id = aws_security_group.service.id
    backend_target_group_arn  = aws_lb_target_group.backend.arn
    entry_target_group_arn    = aws_lb_target_group.service_entry.arn
    cloudfront_domain         = aws_cloudfront_distribution.portal.domain_name
    cognito_pool_id           = aws_cognito_user_pool.portal.id
    cognito_client_id         = aws_cognito_user_pool_client.portal.id
    scheduler_group           = aws_scheduler_schedule_group.portal.name
    scheduler_role_arn        = aws_iam_role.scheduler.arn
    schedule_runner_role_arn  = aws_iam_role.schedule_runner.arn
    schedule_runner_log_group = aws_cloudwatch_log_group.schedule_runner.name
    schedule_runner_function  = local.schedule_runner_function_name
    schedule_dlq_arn          = aws_sqs_queue.schedule_dlq.arn
    service_entry_secret_name = aws_secretsmanager_secret.service_entry.name
    service_entry_api_id      = aws_api_gateway_rest_api.service_entry.id
    portal_admin_secret_name  = local.portal_admin_secret
  }
}
