# team_auth

Port of TeamAuthStack: the external IdP (Keycloak) and three team-scoped backend APIs, all behind one ALB + CloudFront. The containers run on the platform's EKS cluster (workloads.tf).

The full rationale is in the comments of `main.tf` and the module's other files.

<!-- BEGIN_TF_DOCS -->
## Inputs

| Name | Description | Type | Default | Required |
| ---- | ----------- | ---- | ------- | :------: |
| cf\_log\_destination\_arn | ARN of the us-east-1 CloudWatch Logs delivery destination the distribution's standard-logging delivery attaches to. | `string` | n/a | yes |
| eks | Cluster facts from the eks module (see modules/portal/variables.tf). | <pre>object({<br/>    cluster_name              = string<br/>    cluster_security_group_id = string<br/>    oidc_provider_arn         = string<br/>    oidc_issuer_host          = string<br/>    log_group_prefix          = string<br/>    controllers_ready         = string<br/>  })</pre> | n/a | yes |
| keycloak\_image\_tag | Image tag for the Keycloak deployment, pulled from the keycloak repository; the root module falls back to team\_auth\_image\_tag. | `string` | n/a | yes |
| log\_bucket | Name and ARN of the platform access-log bucket; the ALB writes its access logs there under team-auth-alb/. | <pre>object({<br/>    name = string<br/>    arn  = string<br/>  })</pre> | n/a | yes |
| private\_subnet\_ids | Private subnets for the Keycloak RDS PostgreSQL subnet group. | `list(string)` | n/a | yes |
| public\_subnet\_ids | Public subnets for the internet-facing team-auth ALB, the CloudFront origin. | `list(string)` | n/a | yes |
| team\_auth\_image\_tag | Image tag for the team-api deployments, pulled from the team-api repository. | `string` | n/a | yes |
| team\_auth\_repos | ECR repositories for the team-auth images as {url, arn}, keyed keycloak and team-api (modules/platform team\_auth\_repos). | <pre>map(object({<br/>    url = string<br/>    arn = string<br/>  }))</pre> | n/a | yes |
| vpc\_id | VPC holding the team-auth ALB and the Keycloak database, plus their security groups and target groups. | `string` | n/a | yes |
| custom\_domain | Custom domain {name, certificate\_arn} for the distribution, with an ACM certificate in us-east-1; it also becomes Keycloak's public hostname. null keeps the *.cloudfront.net default domain. | <pre>object({<br/>    name            = string<br/>    certificate_arn = string<br/>  })</pre> | `null` | no |
| db\_kms\_key\_arn | KMS key for the restored Keycloak database's storage, required when restoring a snapshot shared from another account. null uses the aws/rds default key; ignored without db\_snapshot\_identifier. | `string` | `null` | no |
| db\_snapshot\_identifier | RDS snapshot (ARN or identifier; ARN if shared from another account) the Keycloak database is created from. null creates an empty database that the realm import seeds. | `string` | `null` | no |
| name\_suffix | Environment suffix appended to the module's resource names (ALB, target groups, security groups, secrets, RDS instance, Kubernetes namespace); empty for production. | `string` | `""` | no |
| web\_acl\_arn | WAFv2 web ACL ARN (CLOUDFRONT scope) attached to the distribution, from modules/edge\_waf. null attaches no web ACL. | `string` | `null` | no |

## Outputs

| Name | Description |
| ---- | ----------- |
| admin\_secret\_arn | ARN of the Secrets Manager secret holding the Keycloak bootstrap admin username and password. |
| alb\_dns\_name | DNS name of the internet-facing team-auth ALB, the distribution's origin. |
| base\_url | Public HTTPS base URL of the distribution serving Keycloak and the team APIs: the custom domain when set, otherwise the *.cloudfront.net domain. |
| discovery\_url | OIDC discovery URL of the agent-platform realm (issuer\_url/.well-known/openid-configuration), which the team-demo runtime's JWT authorizer validates against. |
| distribution\_domain | The distribution's *.cloudfront.net domain (the CNAME target when a custom domain is set). |
| issuer\_url | OIDC issuer URL of the Keycloak agent-platform realm (base\_url/realms/agent-platform). |
| team\_api\_mcp\_urls | MCP endpoint URL of each team API (team-a, team-b, team-c), keyed by team, under base\_url. |
| team\_c\_key\_secret\_arn | ARN of the Secrets Manager secret holding the static API key of the team-c API, which the gateway injects as X-Api-Key. |
<!-- END_TF_DOCS -->
