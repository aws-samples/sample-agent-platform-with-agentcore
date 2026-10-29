# runtime

Execution roles — one per kernel, each holding only what that kernel's code actually calls (docs/permissions.md §2).

The full rationale is in the comments of `iam.tf` and the module's other files.

<!-- BEGIN_TF_DOCS -->
## Inputs

| Name | Description | Type | Default | Required |
| ---- | ----------- | ---- | ------- | :------: |
| kernel\_repos | Map of repo name -> {url, arn} from the platform module (the roles' ECR pull grants) | <pre>map(object({<br/>    url = string<br/>    arn = string<br/>  }))</pre> | n/a | yes |
| workspace\_bucket | Workspace bucket from the platform module. The kernel roles get skills/ reads and the async artifact prefixes; the workspace-access role gets workspaces/. | <pre>object({<br/>    name = string<br/>    arn  = string<br/>  })</pre> | n/a | yes |
| agent\_observability | Grant the sdk kernel's telemetry IAM statements and create the X-Ray trace delivery destination (observability image variant only). | `bool` | `false` | no |
| async\_artifact\_prefixes | S3 key prefixes the headless (SDK) kernel may write async task outputs to. Add your own pipelines' output prefixes. | `list(string)` | <pre>[<br/>  "feeds"<br/>]</pre> | no |
| deny\_gateway\_arns | Gateway ARNs (wildcards allowed) the kernel roles are explicitly denied, overriding their gateway/* InvokeGateway allow. Used for the agentcore\_gateway model backend's inference gateway. | `list(string)` | `[]` | no |
| name\_suffix | Environment suffix appended to role and delivery-destination names; empty for production. | `string` | `""` | no |
| sample\_bedrock\_models | Global inference profile IDs (global.<model>) a sample runtime role may invoke directly, for deployments without the agentcore\_gateway model backend. Each becomes the three-part global cross-Region grant: the profile in this Region, and the foundation model in this Region and region-less, both only through that profile. Empty = no direct Bedrock on sample roles (the gateway deployment). | `list(string)` | `[]` | no |
| sample\_runtimes | Sample agents that get their own runtime and execution role (Sample Agents feature), by sample id (lowercase letters, digits, hyphens). The workloads root creates one runtime per key with the headless kernel image. | `map(object({}))` | `{}` | no |

## Outputs

| Name | Description |
| ---- | ----------- |
| interactive\_role\_arn | Execution role of the claude-code-kernel (Dev Workbench) runtime. |
| mcp\_tools\_role\_arn | Execution role of the mcp-tools-kernel runtime (demo MCP server, no S3 access). |
| sample\_role\_arns | Execution role per sample agent runtime, by sample id. |
| sdk\_role\_arn | Execution role of the agent-sdk-kernel (headless) runtimes; the team\_demo runtime runs under it too. |
| workspace\_access\_role\_arn | Role the backend assumes per session, with a session policy that narrows it to that session's workspaces/ prefix. The portal module grants the backend AssumeRole on it. |
| xray\_delivery\_destination\_arn | Trace delivery destination for the headless runtimes' delivery sources; null when observability is off. |
<!-- END_TF_DOCS -->
