# llm_edge

llm-edge — the platform-side hop that owns the LLM gateway key.

The full rationale is in the comments of `main.tf` and the module's other files.

<!-- BEGIN_TF_DOCS -->
## Inputs

| Name | Description | Type | Default | Required |
| ---- | ----------- | ---- | ------- | :------: |
| eks | Cluster facts from the eks module (see modules/portal/variables.tf). | <pre>object({<br/>    cluster_name              = string<br/>    cluster_security_group_id = string<br/>    oidc_provider_arn         = string<br/>    oidc_issuer_host          = string<br/>    log_group_prefix          = string<br/>    controllers_ready         = string<br/>  })</pre> | n/a | yes |
| llm\_gateway\_secret | Secrets Manager secret holding the upstream gateway API key. This module's workload role is the only kernel-path principal granted read on it. | <pre>object({<br/>    name = string<br/>    arn  = string<br/>  })</pre> | n/a | yes |
| log\_bucket | Access-log bucket from the platform module; the internal ALB writes to it under llm-edge-alb/. | <pre>object({<br/>    name = string<br/>    arn  = string<br/>  })</pre> | n/a | yes |
| platform\_table | Shared platform DynamoDB table — the edge reads session token items and nothing else | <pre>object({<br/>    name = string<br/>    arn  = string<br/>  })</pre> | n/a | yes |
| private\_subnet\_ids | Private subnets the internal ALB is placed in. | `list(string)` | n/a | yes |
| runtime\_sg\_id | SG attached to the AgentCore runtime ENIs — the only source allowed to reach the edge listener | `string` | n/a | yes |
| vpc\_id | VPC of the internal ALB, its target group and the edge's security groups. | `string` | n/a | yes |
| certificate\_arn | ACM certificate for the internal listener. When empty the listener serves<br/>plain HTTP inside the VPC, which is the default because a private listener<br/>needs a certificate for a name the deployment owns and that cannot be<br/>assumed. Supply a certificate to encrypt the shim-to-edge leg; the request<br/>bodies on it carry prompt content, so a deployment handling regulated data<br/>should set this. | `string` | `""` | no |
| name\_suffix | Environment suffix appended to resource names and the Kubernetes namespace (llm-edge<suffix>); empty for production. | `string` | `""` | no |

## Outputs

| Name | Description |
| ---- | ----------- |
| alb\_sg\_id | Security group of the internal ALB; it admits only runtime\_sg\_id on the listener port. |
| edge\_url | Base URL session kernels send model traffic to. Internal ALB: resolvable and reachable only inside the VPC. |
| role\_arn | IRSA role of the llm-edge pods (ServiceAccount edge): reads the gateway secret and session token items. Also published in workload\_facts. |
| workload\_facts | Published to the foundation facts parameter for terraform/workloads/modules/llm\_edge. |
<!-- END_TF_DOCS -->
