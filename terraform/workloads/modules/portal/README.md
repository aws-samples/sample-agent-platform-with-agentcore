# portal

The backend on EKS, workloads half: two Deployments of one image. `backend` serves the management console's API; `entry` runs in ENTRY_ONLY mode and mounts only the IAM service entry for published agents, so agent traffic never goes through the console's rollout. Also the schedule-runner Lambda. The IRSA role, load balancers, target groups, security group, Cognito and scheduler group are the foundation's and arrive in `facts`; this module decides the image, the replicas and the environment the code sees.

The full rationale is in the comments of `main.tf`.

<!-- BEGIN_TF_DOCS -->
## Inputs

| Name | Description | Type | Default | Required |
| ---- | ----------- | ---- | ------- | :------: |
| account\_id | AWS account ID, for the service-entry execute-api ARN base the backend authorizes callers against. | `string` | n/a | yes |
| backend\_image\_tag | Image tag of the backend container. | `string` | n/a | yes |
| backend\_repo\_url | ECR repository URL of the backend image (from the foundation). | `string` | n/a | yes |
| controllers\_ready | Inert token from the foundation that orders the releases after the cluster controllers (their CRDs must exist before a TargetGroupBinding can be applied). | `string` | n/a | yes |
| default\_platform\_version | AgentCore platform version (V1 Standard or V2 Fast Runtime) a session or published agent gets when it does not choose one. | `string` | n/a | yes |
| oidc | Enterprise-SSO settings as the foundation publishes them (issuer empty = Cognito-only). | <pre>object({<br/>    issuer    = string<br/>    client_id = string<br/>    audience  = string<br/>  })</pre> | n/a | yes |
| platform\_table\_name | Name of the platform DynamoDB table (from the foundation). | `string` | n/a | yes |
| portal | The foundation's portal facts (modules/portal output workload\_facts). | <pre>object({<br/>    namespace                 = string<br/>    backend_role_arn          = string<br/>    service_security_group_id = string<br/>    backend_target_group_arn  = string<br/>    entry_target_group_arn    = string<br/>    cloudfront_domain         = string<br/>    # the Cognito pool the backend verifies against when there is no OIDC<br/>    # issuer; a foundation on OIDC may publish none<br/>    cognito_pool_id           = optional(string, "")<br/>    cognito_client_id         = optional(string, "")<br/>    scheduler_group           = string<br/>    scheduler_role_arn        = string<br/>    schedule_runner_role_arn  = string<br/>    schedule_runner_log_group = string<br/>    schedule_runner_function  = string<br/>    schedule_dlq_arn          = string<br/>    service_entry_secret_name = string<br/>    service_entry_api_id      = string<br/>  })</pre> | n/a | yes |
| runtimes | Runtime ARNs from the runtime module (same root). | <pre>object({<br/>    interactive_runtime_arn  = string<br/>    sdk_runtime_arn          = string<br/>    mcp_tools_runtime_arn    = string<br/>    interactive_runtime_arns = map(string)<br/>    sdk_runtime_arns         = map(string)<br/>    # one per sample agent (Sample Agents feature); {} when off<br/>    sample_runtime_arns = optional(map(string), {})<br/>    # the sample's own memory store, for samples the foundation gives memory<br/>    sample_memory_ids = optional(map(string), {})<br/>  })</pre> | n/a | yes |
| schedule\_runner | Where the schedule-runner Lambda runs and how it reaches the backend: the platform VPC's private subnets and a security group (the private service entry is reachable only from inside the VPC), and the entry's endpoint-specific URL ending in /svc/. Required: the runner has no other way to the backend. | <pre>object({<br/>    subnet_ids        = list(string)<br/>    security_group_id = string<br/>    service_url       = string<br/>  })</pre> | n/a | yes |
| workspace\_access\_role\_arn | Role the backend assumes to reach session workspaces in the bucket (from the foundation). | `string` | n/a | yes |
| workspace\_bucket\_name | Name of the S3 workspace bucket (from the foundation). | `string` | n/a | yes |
| agentcore\_gateway\_caller\_role\_arn | Caller role for the agentcore\_gateway model backend. Empty = that backend is unavailable and the backend refuses it. | `string` | `""` | no |
| backend\_desired\_count | Replicas of the management backend Deployment. | `number` | `2` | no |
| entry\_desired\_count | Replicas of the data-plane (ENTRY\_ONLY) Deployment behind the private service-entry API. | `number` | `1` | no |
| lark\_ingress | The foundation's Lark ingress facts (lark\_ingress), or null when the foundation has none (enable\_lark\_ingress off). Creates the lark-webhook function for the lark-teammate sample. | <pre>object({<br/>    function_name = string<br/>    role_arn      = string<br/>    log_group     = string<br/>    secret_arn    = string<br/>    open_base     = string<br/>    # the ambient-memory function; "" when the foundation has the jobs off<br/>    memory_function_name = optional(string, "")<br/>    memory_role_arn      = optional(string, "")<br/>    memory_log_group     = optional(string, "")<br/>    ambient_model        = optional(string, "")<br/>    # the reminders table; "" when the foundation has reminders off<br/>    reminders_table  = optional(string, "")<br/>    utc_offset_hours = optional(number, 8)<br/>    # the function may remember and forget on the sample's store<br/>    memory_notes = optional(bool, false)<br/>    # the ingress's /lark/mcp route; "" when the foundation has the Lark tools off<br/>    mcp_url = optional(string, "")<br/>    # the turn's agent may use the code sandbox and send the files it makes<br/>    sandbox = optional(bool, false)<br/>  })</pre> | `null` | no |
| llm\_edge\_url | Internal base URL of the llm-edge service. Empty means gateway-mode model routing is unavailable, and the backend rejects it rather than handing the gateway key to a container. | `string` | `""` | no |
| mcp\_hub\_caller\_role\_arn | Caller role of the MCP hub IAM entry (foundation facts mcp\_hub.caller\_role\_arn). Empty = iam hub attachments are unavailable and the backend refuses them. | `string` | `""` | no |
| name\_suffix | Environment suffix appended to this module's names (empty in production, -staging in staging). | `string` | `""` | no |
| sample\_tools\_gateway\_url | MCP URL of the foundation's sample-tools gateway, which the backend registers for the sample agents; "" when the feature is off. | `string` | `""` | no |
| sandbox | One-shot code sandboxes: Standard Runtime ARN by image version ({} = feature off), Fast Runtime ARN by version for those that have one, the default version, the lifecycle the runtimes run with, and the IAM callers allowed on the service API. | <pre>object({<br/>    versions        = map(string)<br/>    fast_versions   = optional(map(string), {})<br/>    default_version = string<br/>    idle_timeout_s  = number<br/>    max_lifetime_s  = number<br/>    service_callers = list(string)<br/>  })</pre> | <pre>{<br/>  "default_version": "",<br/>  "idle_timeout_s": 300,<br/>  "max_lifetime_s": 2400,<br/>  "service_callers": [],<br/>  "versions": {}<br/>}</pre> | no |

## Outputs

| Name | Description |
| ---- | ----------- |
| namespace | Kubernetes namespace the backend runs in. |
| release\_names | Helm releases this module owns, as namespace/name (the helm provider's import id). |
| schedule\_runner\_function | Name of the schedule-runner Lambda function. |
| schedule\_runner\_function\_arn | ARN of the schedule-runner Lambda function. |
<!-- END_TF_DOCS -->
