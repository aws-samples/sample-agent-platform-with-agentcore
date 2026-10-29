# mcp_hub_demo

MCP hub demo: a customer-owned MCP hub replacing AgentCore Gateway as the tool backend, plus an EC2 that plays the calling application — so the whole story is walkable end to end:

The full rationale is in the comments of `main.tf` and the module's other files.

<!-- BEGIN_TF_DOCS -->
## Inputs

| Name | Description | Type | Default | Required |
| ---- | ----------- | ---- | ------- | :------: |
| backend\_role\_arn | Backend IRSA role: mints the hub caller sessions (plain AssumeRole path, used off EKS). | `string` | n/a | yes |
| backend\_service\_accounts | IRSA subjects (system:serviceaccount:<ns>:<sa>) of the backend pods that may exchange their web-identity token for the hub caller role. | `list(string)` | n/a | yes |
| eks | EKS IRSA facts: the caller role trusts the backend's ServiceAccount web-identity token directly (first hop, up to MaxSessionDuration). | <pre>object({<br/>    oidc_provider_arn = string<br/>    oidc_issuer_host  = string<br/>  })</pre> | n/a | yes |
| keycloak\_issuer\_url | The platform realm's issuer URL (team\_auth module output). | `string` | n/a | yes |
| private\_subnet\_ids | Private subnets: the entry NLB spans all of them, and the hub and demo-app instances go in the first. | `list(string)` | n/a | yes |
| runtime\_sg\_id | Security group the AgentCore runtimes egress from — the only ingress the hub admits. | `string` | n/a | yes |
| service\_api\_execution\_arn | execute-api ARN base of the service-entry API (…:api-id), for the demo app role's Invoke grant. | `string` | n/a | yes |
| service\_api\_url | Invoke URL of the private service-entry API, for the demo app. | `string` | n/a | yes |
| service\_api\_vpce\_id | The platform VPC's execute-api interface endpoint: the only path into the hub entry API (resource policy + association). | `string` | n/a | yes |
| vpc\_cidr\_block | Platform VPC CIDR: the entry NLB admits MCP traffic from inside the VPC only. | `string` | n/a | yes |
| vpc\_id | VPC of the hub and demo-app instances, the entry NLB and their security groups. | `string` | n/a | yes |
| workspace\_bucket | Workspace bucket from the platform module; the hub instance downloads its source zip from the mcp-hub/ prefix. | <pre>object({<br/>    name = string<br/>    arn  = string<br/>  })</pre> | n/a | yes |
| app\_instance\_type | EC2 instance type of the demo calling-application host. Must be arm64 (Graviton): the AMI is AL2023 arm64. | `string` | `"t4g.micro"` | no |
| hub\_instance\_type | EC2 instance type of the MCP hub host. Must be arm64 (Graviton): the AMI is AL2023 arm64. | `string` | `"t4g.small"` | no |
| hub\_resource\_url | Logical resource identifier the hub announces and tokens must carry as<br/>their audience. Deliberately NOT the instance's DNS name, so replacing<br/>the instance never invalidates issued tokens or the Keycloak audience<br/>mapper — the registry entry's target (the real endpoint) is what moves. | `string` | `"http://mcp-hub.agent-platform.internal/mcp"` | no |
| hub\_source\_s3\_key | Key (in the workspace bucket) of the packaged MCP hub source zip — see scripts/package\_mcp\_hub.sh. | `string` | `"mcp-hub/source.zip"` | no |
| name\_suffix | Environment suffix appended to instance, role, secret and API names; empty for production. | `string` | `""` | no |

## Outputs

| Name | Description |
| ---- | ----------- |
| app\_client\_secret\_name | Secrets Manager name the seed script writes the app's IdP client credentials to. |
| app\_credentials\_secret\_name | Same as app\_client\_secret\_name, under the name the foundation facts publish it as. |
| app\_instance\_id | EC2 instance ID of the demo calling-application host; published in the foundation facts for the seed and e2e scripts. |
| app\_role\_arn | Allowlist this on the platform's iam channel — it is the calling application's identity. |
| hub\_caller\_role\_arn | The role the kernels assume per agent to call the entry (session name = actor). |
| hub\_endpoint | The hub's real MCP endpoint — this is what the platform registry entry targets. |
| hub\_entry\_api\_id | ID of the private REST API in front of the hub (the IAM entry); hub\_entry\_url is built from it. |
| hub\_entry\_url | The hub's IAM entry: the private API's endpoint-specific invoke URL (resolvable without private DNS on the endpoint). Register this as an mcp-hub target with auth = iam. |
| hub\_instance\_id | EC2 instance ID of the MCP hub host; published in the foundation facts for the seed and e2e scripts. |
| hub\_resource\_url | Logical resource identifier / token audience the hub enforces. |
<!-- END_TF_DOCS -->
