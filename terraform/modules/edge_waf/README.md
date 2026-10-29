# edge_waf

Edge WAF: one CLOUDFRONT-scope web ACL per environment, shared by the portal and team-auth distributions. CloudFront, not the ALBs: an ALB caps body inspection at 8 KB with no way to raise it, and the ALBs only admit the CloudFront origin-facing prefix list plus x-origin-verify anyway, so every request that reaches an origin has already passed this ACL.

The full rationale is in the comments of `main.tf` and the module's other files.

<!-- BEGIN_TF_DOCS -->
## Inputs

| Name | Description | Type | Default | Required |
| ---- | ----------- | ---- | ------- | :------: |
| platform\_egress\_ips | The platform's own egress IPs (the NAT EIP) as bare IPv4 addresses, no prefix length. Their Keycloak discovery and JWKS reads skip the origin rate limit; the managed rules still apply. | `list(string)` | n/a | yes |
| name\_suffix | Environment suffix appended to the web ACL, IP set and log group names; empty for production. | `string` | `""` | no |
| rate\_limit\_credentials\_per\_5min | Requests per IP per 5 minutes on Keycloak's token endpoint and login-form actions before the WAF blocks that IP. | `number` | `200` | no |
| rate\_limit\_per\_5min | Requests per IP per 5 minutes on the origin paths before the WAF blocks that IP. | `number` | `1500` | no |

## Outputs

| Name | Description |
| ---- | ----------- |
| log\_group | Name of the us-east-1 CloudWatch log group (aws-waf-logs-...) that receives the web ACL's logs. |
| web\_acl\_arn | ARN of the CLOUDFRONT-scope web ACL; the portal and team\_auth distributions take it as web\_acl\_id. |
<!-- END_TF_DOCS -->
