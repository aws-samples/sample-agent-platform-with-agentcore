output "subnet_ids" {
  value = aws_subnet.sandbox[*].id
}

output "security_group_id" {
  value = aws_security_group.sandbox.id
}

output "role_arn" {
  value = aws_iam_role.sandbox.arn
}

output "repository_url" {
  value = var.repository_url
}

output "repository_arn" {
  value = var.repository_arn
}

output "s3_endpoint_id" {
  value = aws_vpc_endpoint.s3.id
}
