output "workspace_bucket" {
  value = {
    name = aws_s3_bucket.workspace.bucket
    arn  = aws_s3_bucket.workspace.arn
  }
}

output "platform_table" {
  value = {
    name = aws_dynamodb_table.platform.name
    arn  = aws_dynamodb_table.platform.arn
  }
}

output "kernel_repos" {
  value = {
    for k, r in aws_ecr_repository.kernel : k => {
      url = r.repository_url
      arn = r.arn
    }
  }
}

output "team_auth_repos" {
  value = {
    for k, r in aws_ecr_repository.team_auth : k => {
      url = r.repository_url
      arn = r.arn
    }
  }
}

output "llm_gateway_secret" {
  value = {
    name = aws_secretsmanager_secret.llm_gateway.name
    arn  = aws_secretsmanager_secret.llm_gateway.arn
  }
}
