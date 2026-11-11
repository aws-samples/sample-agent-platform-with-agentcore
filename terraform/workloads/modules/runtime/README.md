# runtime

The AgentCore runtimes of the interactive (Claude Code) kernel, the SDK kernel, the MCP tools kernel and one per sample agent, workloads half. Execution roles, subnets and the security group come from the foundation facts; this module decides which image each runtime runs and how it is configured. The interactive and SDK kernels get one runtime per platform version (Standard Runtime = V1, Fast Runtime = V2), since AgentCore sets the platform version on the runtime itself.

The full rationale is in the comments of `main.tf`, `platform_version.tf`, `log_retention.tf` and `observability.tf`.

<!-- BEGIN_TF_DOCS -->
## Inputs

| Name | Description | Type | Default | Required |
| ---- | ----------- | ---- | ------- | :------: |
| kernel\_repos | Map of repo name -> {url} published by the foundation | <pre>map(object({<br/>    url = string<br/>  }))</pre> | n/a | yes |
| kernel\_tags | Image tag per kernel repo | `map(string)` | n/a | yes |
| model\_env | Model settings passed to the kernels as environment variables (CLAUDE\_CODE\_USE\_BEDROCK, ANTHROPIC\_MODEL, ANTHROPIC\_SMALL\_FAST\_MODEL and the /model alias targets); an empty string leaves that variable unset. | <pre>object({<br/>    use_bedrock                    = string<br/>    anthropic_model                = string<br/>    anthropic_small_fast_model     = string<br/>    anthropic_default_opus_model   = string<br/>    anthropic_default_sonnet_model = string<br/>    anthropic_default_haiku_model  = string<br/>  })</pre> | n/a | yes |
| private\_subnet\_ids | Private subnets the runtimes' ENIs are placed in (VPC network mode). | `list(string)` | n/a | yes |
| roles | Execution role ARNs the foundation created: the three kernels, and one per sample agent runtime (samples, by sample id; {} when the Sample Agents feature is off). | <pre>object({<br/>    interactive = string<br/>    sdk         = string<br/>    mcp_tools   = string<br/>    samples     = optional(map(string), {})<br/>  })</pre> | n/a | yes |
| runtime\_sg\_id | Security group attached to the runtimes' ENIs (from the foundation). | `string` | n/a | yes |
| workspace\_bucket\_name | Name of the S3 workspace bucket the kernels sync session files with. | `string` | n/a | yes |
| agent\_observability | Create the sdk kernel's trace delivery sources (observability image variant only). | `bool` | `false` | no |
| default\_platform\_version | Platform version used when a session or published agent does not choose one. Must be in platform\_versions. | `string` | `"V1"` | no |
| mcp\_tools\_platform\_version | Platform version of the MCP tools runtime (a single runtime: it is a gateway target, not chosen per session). | `string` | `"V1"` | no |
| name\_suffix | Environment suffix appended to this module's resource names (empty in production, -staging in staging); runtime names use runtime\_name\_suffix. | `string` | `""` | no |
| platform\_version\_python | Python interpreter for scripts/set\_platform\_version.py (needs boto3/botocore >= 1.43.98, the first release with platformVersion). | `string` | `"python3"` | no |
| platform\_versions | AgentCore Runtime platform versions to deploy the interactive and headless kernels on. One runtime per kernel per version, same image. V2 is available only in some Regions (see the AgentCore Runtime docs). | `list(string)` | <pre>[<br/>  "V1"<br/>]</pre> | no |
| runtime\_log\_retention\_days | Retention in days for the runtimes' /aws/bedrock-agentcore/runtimes/<id>-DEFAULT log groups (kernel stdout, and prompt/answer event records when agent\_observability is on). Must be a CloudWatch Logs retention value; 0 leaves the groups as AgentCore created them (never expire). | `number` | `30` | no |
| runtime\_name\_suffix | name\_suffix with hyphens replaced by underscores (AgentCore runtime names allow only [a-zA-Z0-9\_]) | `string` | `""` | no |
| sample\_platform\_version | Platform version of every sample agent runtime (one runtime per sample). | `string` | `"V1"` | no |
| xray\_delivery\_destination\_arn | The foundation's X-Ray delivery destination (null when observability is off there). | `string` | `null` | no |

## Outputs

| Name | Description |
| ---- | ----------- |
| interactive\_runtime\_arn | ARN of the default platform version's interactive runtime, for consumers that know one ARN per kernel. |
| interactive\_runtime\_arns | Interactive kernel runtime ARNs by platform version ({V1 = arn, V2 = arn}); the backend routes each session or published agent to one of them. |
| mcp\_tools\_runtime\_arn | ARN of the MCP tools runtime. |
| runtime\_ids | agent\_runtime\_id by runtime name, for operators and the import tooling. |
| sample\_runtime\_arns | One runtime ARN per sample agent ({<sample id> = arn}); the backend routes an agent cloned from a sample to its sample's runtime. |
| sdk\_runtime\_arn | ARN of the default platform version's SDK runtime. |
| sdk\_runtime\_arns | SDK kernel runtime ARNs by platform version. |
<!-- END_TF_DOCS -->
