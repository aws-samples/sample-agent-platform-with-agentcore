# sample_tools

The sample-tools gateway of the Sample Agents feature: the identity and policy shell for the tools a sample agent may call. One per environment.

The full rationale is in the comments of `main.tf` and the module's other files.

<!-- BEGIN_TF_DOCS -->
## Inputs

| Name | Description | Type | Default | Required |
| ---- | ----------- | ---- | ------- | :------: |
| credential\_provider\_arns | Explicit API-key credential providers for the gateway's OpenAPI targets, by tool package name (e.g. stock-quote), on top of the ones discovered by name (<env prefix>sample-tool-<tool>, scripts/set\_sample\_tool\_key.sh). Normally empty. Created out of band so the key never enters Terraform state; the gateway role may read these providers' keys and no others. A tool whose provider is absent is deployed without its target by the workloads root. | `map(string)` | `{}` | no |
| name\_suffix | Environment suffix appended to the gateway, role and policy engine names (hyphens become underscores for the engine) and folded into the credential-provider name prefix; empty for production. | `string` | `""` | no |
| policy\_mode | Enforcement mode of the gateway's policy engine. LOG\_ONLY traces every decision without enforcing; ENFORCE denies what no Cedar policy permits. | `string` | `"LOG_ONLY"` | no |
| script\_python | Python interpreter for scripts/discover\_sample\_tool\_providers.py (needs boto3 with bedrock-agentcore-control). | `string` | `"python3"` | no |

## Outputs

| Name | Description |
| ---- | ----------- |
| credential\_provider\_arns | The tools' providers, by tool package: discovered by name plus the explicit ones. The workloads root creates an OpenAPI target only for a tool whose provider is here. |
| credential\_provider\_prefix | Name prefix of this environment's discovered providers (scripts/set\_sample\_tool\_key.sh creates <prefix><tool>). |
| gateway\_arn | ARN of the Sample Agents tool gateway. |
| gateway\_id | ID of the Sample Agents MCP tool gateway (IAM-authorized). |
| gateway\_url | MCP URL of the Sample Agents tool gateway; the backend registers it as an MCP server for the sample runtimes. |
| lambda\_role\_arn | Execution role of the sample tool Lambdas the workloads root creates. |
| policy\_engine\_arn | ARN of the gateway's Cedar policy engine. |
| policy\_engine\_id | ID of the Cedar policy engine attached to the gateway; the workloads root adds its policies to it. |
| policy\_mode | LOG\_ONLY or ENFORCE: how the gateway treats the engine's decisions. The acceptance checks read it to know whether a denied caller is refused or only logged. |
<!-- END_TF_DOCS -->
