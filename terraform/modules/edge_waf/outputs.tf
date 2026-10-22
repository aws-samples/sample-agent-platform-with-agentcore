# aws_cloudfront_distribution.web_acl_id takes the ARN for WAFv2 ACLs.
output "web_acl_arn" {
  value = aws_wafv2_web_acl.edge.arn
}

output "log_group" {
  value = aws_cloudwatch_log_group.waf.name
}
