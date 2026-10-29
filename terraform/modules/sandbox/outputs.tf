output "subnet_ids" {
  description = "IDs of the sandbox subnets (no default route) the workloads root places the sandbox runtime in."
  value       = aws_subnet.sandbox[*].id
}

output "security_group_id" {
  description = "ID of the sandbox security group (no ingress; egress only to the VPC endpoints, S3 and the callback targets) the sandbox runtime runs under."
  value       = aws_security_group.sandbox.id
}

output "role_arn" {
  description = "ARN of the sandbox execution role, which can only pull the sandbox image and write the sandbox runtime's logs."
  value       = aws_iam_role.sandbox.arn
}

output "repository_url" {
  description = "URL of the sandbox-kernel ECR repository the sandbox runtime image is pulled from; passed through from var.repository_url."
  value       = var.repository_url
}

output "repository_arn" {
  description = "ARN of the sandbox-kernel ECR repository; passed through from var.repository_arn."
  value       = var.repository_arn
}

output "s3_endpoint_id" {
  description = "ID of the sandbox's own S3 gateway endpoint, whose policy admits only the ECR layer bucket and the state-snapshot objects."
  value       = aws_vpc_endpoint.s3.id
}
