output "portal_url" {
  description = "Public portal URL (https://), on the custom domain when one is set, otherwise the distribution's *.cloudfront.net domain."
  value       = "https://${local.public_host}"
}

output "distribution_id" {
  description = "ID of the CloudFront distribution in front of the portal, invalidated after each frontend publish."
  value       = aws_cloudfront_distribution.portal.id
}

output "frontend_bucket_name" {
  description = "Name of the S3 bucket serving the portal's static frontend build (the distribution's S3 origin)."
  value       = aws_s3_bucket.frontend.bucket
}

output "alb_dns_name" {
  description = "DNS name of the public portal ALB, the distribution's API origin."
  value       = aws_lb.portal.dns_name
}

output "user_pool_id" {
  description = "ID of the Cognito user pool for portal sign-in (self-signup disabled; operators create users)."
  value       = aws_cognito_user_pool.portal.id
}

output "user_pool_client_id" {
  description = "ID of the Cognito app client the portal frontend signs in with."
  value       = aws_cognito_user_pool_client.portal.id
}

output "schedule_runner_function" {
  description = "Fixed name of the schedule-runner Lambda (the function itself is a workload)."
  value       = local.schedule_runner_function_name
}

output "schedule_dlq_url" {
  description = "URL of the SQS dead-letter queue for EventBridge Scheduler deliveries to the schedule-runner Lambda that failed (14-day retention)."
  value       = aws_sqs_queue.schedule_dlq.url
}

# The operator creates this secret by hand (see docs/deployment.md); the name
# is suffix-dependent, so read it from here rather than typing it.
output "portal_admin_secret_name" {
  description = "Name of the portal-admin Secrets Manager secret the operator creates by hand; suffix-dependent, so read it from here."
  value       = local.portal_admin_secret
}

output "service_entry_api_url" {
  description = "Regional invoke URL of the private service-entry API's svc stage; reachable only through an associated interface VPC endpoint."
  value       = "https://${aws_api_gateway_rest_api.service_entry.id}.execute-api.${local.region}.amazonaws.com/svc/"
}

output "service_entry_api_id" {
  description = "REST API ID of the private service-entry API; root main.tf builds the endpoint-specific invoke URL for mcp_hub_demo from it."
  value       = aws_api_gateway_rest_api.service_entry.id
}

output "service_entry_api_execution_arn" {
  description = "Execution ARN of the private service-entry API; mcp_hub_demo scopes its execute-api:Invoke grant to it."
  value       = aws_api_gateway_rest_api.service_entry.execution_arn
}

output "namespace" {
  description = "Kubernetes namespace the backend and entry workloads run in (portal plus name_suffix)."
  value       = local.namespace
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
    cloudfront_domain         = local.public_host # the custom domain when set; the CORS origin and API URL the workloads derive from it
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

output "distribution_domain" {
  description = "The distribution's *.cloudfront.net domain (the CNAME target when a custom domain is set)."
  value       = aws_cloudfront_distribution.portal.domain_name
}
