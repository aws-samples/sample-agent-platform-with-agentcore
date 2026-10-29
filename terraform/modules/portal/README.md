# portal

Port of PortalStack (part 1): auth, frontend bucket, CloudFront.

The full rationale is in the comments of `main.tf` and the module's other files.

<!-- BEGIN_TF_DOCS -->
## Inputs

| Name | Description | Type | Default | Required |
| ---- | ----------- | ---- | ------- | :------: |
| cf\_log\_destination\_arn | ARN of the us-east-1 CloudWatch Logs delivery destination (platform module) that the distribution's standard-logging delivery attaches to. | `string` | n/a | yes |
| eks | Cluster facts from the eks module: where the workloads run, the OIDC provider their role trusts, and the cluster security group used for probes/DNS. | <pre>object({<br/>    cluster_name              = string<br/>    cluster_security_group_id = string<br/>    oidc_provider_arn         = string<br/>    oidc_issuer_host          = string<br/>    log_group_prefix          = string<br/>    controllers_ready         = string<br/>  })</pre> | n/a | yes |
| log\_bucket | Name and ARN of the platform access-log bucket; the portal ALB writes its access logs there under the portal-alb prefix. | <pre>object({<br/>    name = string<br/>    arn  = string<br/>  })</pre> | n/a | yes |
| platform\_table | Name and ARN of the shared control-plane DynamoDB table (platform module); the backend and schedule-runner roles are granted access to it and its indexes. | <pre>object({<br/>    name = string<br/>    arn  = string<br/>  })</pre> | n/a | yes |
| private\_subnet\_ids | Private subnets for the internal service-entry NLB, the VPC Link target of the private service-entry API. | `list(string)` | n/a | yes |
| public\_subnet\_ids | Public subnets for the internet-facing portal ALB that CloudFront uses as its API origin. | `list(string)` | n/a | yes |
| vpc\_cidr\_block | CIDR of the platform VPC; the service security group admits it on the container port so the service-entry NLB (VPC Link path) can reach the entry pods. | `string` | n/a | yes |
| vpc\_id | VPC the portal's ALB, internal service-entry NLB, target groups and security groups are created in. | `string` | n/a | yes |
| workspace\_access\_role\_arn | ARN of the runtime module's workspace-access role, which the backend assumes with a session policy narrowing S3 access to one session's workspace prefix. | `string` | n/a | yes |
| workspace\_bucket | Name and ARN of the per-session workspace bucket (platform module); the backend and schedule-runner roles are granted access to it. | <pre>object({<br/>    name = string<br/>    arn  = string<br/>  })</pre> | n/a | yes |
| agentcore\_gateway\_caller\_role\_arn | Caller role for the agentcore\_gateway model backend. Empty = that backend is unavailable and the backend refuses it. | `string` | `""` | no |
| custom\_domain | Custom domain name and its us-east-1 ACM certificate ARN for the distribution. null = the default *.cloudfront.net domain; the DNS record is managed outside Terraform. | <pre>object({<br/>    name            = string<br/>    certificate_arn = string<br/>  })</pre> | `null` | no |
| enable\_agentcore\_gateway\_backend | Grant the backend role what the agentcore\_gateway backend needs on the caller role (a plan-time flag; the role ARN itself is only known after apply). | `bool` | `false` | no |
| enable\_sandbox\_entry | Route /service/v1/sandboxes/* through the service entry (the one-shot code sandbox service API). | `bool` | `false` | no |
| name\_suffix | Environment suffix appended to the portal's resource names (Cognito pool, buckets, load balancers, roles, secrets, Kubernetes namespace); empty for production. | `string` | `""` | no |
| service\_api\_allowed\_vpces | Interface VPC endpoint IDs associated with the private service-entry API and pinned in its resource policy as aws:SourceVpce. Empty = no endpoint association and no SourceVpce condition. | `list(string)` | `[]` | no |
| service\_api\_throttle | Stage-wide throttle (requests per second and burst) of the private service-entry API, shared by every caller and route; passed through from the root variable. | <pre>object({<br/>    rate_limit  = number<br/>    burst_limit = number<br/>  })</pre> | <pre>{<br/>  "burst_limit": 100,<br/>  "rate_limit": 50<br/>}</pre> | no |
| web\_acl\_arn | ARN of the CLOUDFRONT-scope WAFv2 web ACL (modules/edge\_waf) attached to the distribution. null = no web ACL. | `string` | `null` | no |

## Outputs

| Name | Description |
| ---- | ----------- |
| alb\_dns\_name | DNS name of the public portal ALB, the distribution's API origin. |
| distribution\_domain | The distribution's *.cloudfront.net domain (the CNAME target when a custom domain is set). |
| distribution\_id | ID of the CloudFront distribution in front of the portal, invalidated after each frontend publish. |
| frontend\_bucket\_name | Name of the S3 bucket serving the portal's static frontend build (the distribution's S3 origin). |
| namespace | Kubernetes namespace the backend and entry workloads run in (portal plus name\_suffix). |
| portal\_admin\_secret\_name | Name of the portal-admin Secrets Manager secret the operator creates by hand; suffix-dependent, so read it from here. |
| portal\_url | Public portal URL (https://), on the custom domain when one is set, otherwise the distribution's *.cloudfront.net domain. |
| schedule\_dlq\_url | URL of the SQS dead-letter queue for EventBridge Scheduler deliveries to the schedule-runner Lambda that failed (14-day retention). |
| schedule\_runner\_function | Fixed name of the schedule-runner Lambda (the function itself is a workload). |
| service\_entry\_api\_execution\_arn | Execution ARN of the private service-entry API; mcp\_hub\_demo scopes its execute-api:Invoke grant to it. |
| service\_entry\_api\_id | REST API ID of the private service-entry API; root main.tf builds the endpoint-specific invoke URL for mcp\_hub\_demo from it. |
| service\_entry\_api\_url | Regional invoke URL of the private service-entry API's svc stage; reachable only through an associated interface VPC endpoint. |
| user\_pool\_client\_id | ID of the Cognito app client the portal frontend signs in with. |
| user\_pool\_id | ID of the Cognito user pool for portal sign-in (self-signup disabled; operators create users). |
| workload\_facts | Published to the foundation facts parameter for terraform/workloads/modules/portal. |
<!-- END_TF_DOCS -->
