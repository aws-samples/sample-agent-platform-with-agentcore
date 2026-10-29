output "workspace_bucket" {
  description = "Name and ARN of the per-session workspace bucket (files and Claude Code state; skills, async outputs and the MCP hub source zip live under their own prefixes)."
  value = {
    name = aws_s3_bucket.workspace.bucket
    arn  = aws_s3_bucket.workspace.arn
  }
}

output "platform_table" {
  description = "Name and ARN of the shared control-plane DynamoDB table (PK/SK keys). Published by name in the foundation facts."
  value = {
    name = aws_dynamodb_table.platform.name
    arn  = aws_dynamodb_table.platform.arn
  }
}

output "kernel_repos" {
  description = "ECR repositories by name (claude-code-kernel, agent-sdk-kernel, mcp-tools-kernel, backend) as {url, arn}. The kernel execution roles are granted pull on every repo in this map."
  value = {
    for k, r in aws_ecr_repository.kernel : k => {
      url = r.repository_url
      arn = r.arn
    }
  }
}

output "team_auth_repos" {
  description = "ECR repositories for the team_auth images (keycloak, team-api) as {url, arn}, by name."
  value = {
    for k, r in aws_ecr_repository.team_auth : k => {
      url = r.repository_url
      arn = r.arn
    }
  }
}

output "llm_edge_repo" {
  description = "URL and ARN of the llm-edge ECR repository. Kept out of kernel_repos so that no kernel role can pull the image of the service holding the gateway key."
  value = {
    url = aws_ecr_repository.llm_edge.repository_url
    arn = aws_ecr_repository.llm_edge.arn
  }
}

output "sandbox_kernel_repo" {
  description = "URL and ARN of the one-shot code sandbox ECR repository. Kept out of kernel_repos so that kernel execution roles cannot pull the sandbox image."
  value = {
    url = aws_ecr_repository.sandbox_kernel.repository_url
    arn = aws_ecr_repository.sandbox_kernel.arn
  }
}

output "log_bucket" {
  description = "Name and ARN of the access-log bucket for the ALBs and CloudFront distributions. Waits on the bucket policy, which log delivery is validated against."
  value = {
    name = aws_s3_bucket.logs.bucket
    arn  = aws_s3_bucket.logs.arn
  }
  # log delivery (ALB attribute change, CloudWatch delivery creation) is
  # validated against the bucket policy — make consumers wait for it
  depends_on = [aws_s3_bucket_policy.logs]
}

output "cf_log_destination_arn" {
  description = "ARN of the us-east-1 CloudWatch Logs delivery destination (the log bucket) that the portal and team_auth distributions attach their standard-logging deliveries to."
  value       = aws_cloudwatch_log_delivery_destination.cf_logs.arn
  depends_on  = [aws_s3_bucket_policy.logs]
}

output "llm_gateway_secret" {
  description = "Name and ARN of the Secrets Manager secret for the upstream LLM gateway API key. Terraform writes a placeholder value; the real key is put out of band."
  value = {
    name = aws_secretsmanager_secret.llm_gateway.name
    arn  = aws_secretsmanager_secret.llm_gateway.arn
  }
}
