# aws_cloudfront_distribution.web_acl_id takes the ARN for WAFv2 ACLs.
output "web_acl_arn" {
  description = "ARN of the CLOUDFRONT-scope web ACL; the portal and team_auth distributions take it as web_acl_id."
  value       = aws_wafv2_web_acl.edge.arn
}

output "log_group" {
  description = "Name of the us-east-1 CloudWatch log group (aws-waf-logs-...) that receives the web ACL's logs."
  value       = aws_cloudwatch_log_group.waf.name
}
