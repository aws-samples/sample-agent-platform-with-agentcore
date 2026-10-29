# agentcore_gateway_backend

AgentCore Gateway in front of LiteLLM, for the agentcore_gateway model backend (docs/architecture.md, "How the agentcore_gateway backend reaches a private LiteLLM").

The full rationale is in the comments of `main.tf` and the module's other files.

<!-- BEGIN_TF_DOCS -->
## Inputs

| Name | Description | Type | Default | Required |
| ---- | ----------- | ---- | ------- | :------: |
| backend\_role\_arn | Backend IRSA role: allowed to assume the caller role (plain AssumeRole path, used off EKS). | `string` | n/a | yes |
| backend\_service\_accounts | system:serviceaccount:<ns>:<name> subjects allowed to exchange their token for the caller role. | `list(string)` | n/a | yes |
| credential\_provider\_arn | AgentCore Identity API-key credential provider holding the LiteLLM virtual key. Created out of band (console or API) so the key never enters Terraform state. | `string` | n/a | yes |
| eks | EKS IRSA facts. The caller role trusts the backend's ServiceAccount web-identity token directly, so per-session credentials are a first hop (up to MaxSessionDuration) rather than a chained session capped at one hour. | <pre>object({<br/>    oidc_provider_arn = string<br/>    oidc_issuer_host  = string<br/>  })</pre> | n/a | yes |
| litellm\_endpoint | HTTPS base URL of LiteLLM as the gateway target sees it. With a private endpoint this is the name on the internal ALB's publicly trusted certificate (sent as TLS SNI and Host); it needs no public DNS record. | `string` | n/a | yes |
| models | LiteLLM model names the target accepts on every path. Keep in line with what the virtual key allows. | `list(string)` | n/a | yes |
| name\_suffix | Environment suffix appended to the gateway and role names; empty for production. The runtime module's deny on gateway/agent-platform-inference* relies on that name prefix. | `string` | `""` | no |
| private\_endpoint | Reach LiteLLM over a managed VPC Lattice resource gateway instead of the internet. routing\_domain is where Lattice connects (for example an internal ALB's DNS name); subnets/security groups host the resource-gateway ENIs in LiteLLM's VPC. Null = the target dials litellm\_endpoint directly. | <pre>object({<br/>    vpc_id             = string<br/>    subnet_ids         = list(string)<br/>    security_group_ids = list(string)<br/>    routing_domain     = string<br/>  })</pre> | `null` | no |
| script\_python | Python interpreter for scripts/set\_inference\_target.py (needs a botocore that models inference targets and privateEndpoint). | `string` | `"python3"` | no |

## Outputs

| Name | Description |
| ---- | ----------- |
| caller\_role\_arn | Per-session caller role for the agentcore\_gateway model backend. The portal module grants the backend AssumeRole and inline-policy management on it; the foundation facts publish it. |
| gateway\_arn | ARN of the inference gateway, the only resource the caller role may invoke. |
| gateway\_id | ID of the inference gateway; inference\_base\_url is built from it. |
| inference\_base\_url | base\_url for the agentcore\_gateway backend in Governance -> Model backends |
<!-- END_TF_DOCS -->
