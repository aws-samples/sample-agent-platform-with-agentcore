# sample_tools

The Sample Agents tool stack, workloads half. The foundation owns the gateway, its role, the policy engine and the tool Lambdas' role. Per tool package under `samples/tools/<id>/`, this module adds a Lambda target or an OpenAPI target (only when the facts name an API-key credential provider for that tool) and the tool's Cedar policy.

The full rationale is in the comments of `main.tf`.

<!-- BEGIN_TF_DOCS -->
## Inputs

| Name | Description | Type | Default | Required |
| ---- | ----------- | ---- | ------- | :------: |
| gateway\_arn | ARN of the sample-tools AgentCore Gateway the tool Lambdas are registered on (from the foundation). | `string` | n/a | yes |
| gateway\_id | The foundation's sample-tools gateway (facts.sample\_tools.gateway\_id). | `string` | n/a | yes |
| lambda\_role\_arn | Execution role of the sample tool Lambdas (facts.sample\_tools.lambda\_role\_arn): logs only. | `string` | n/a | yes |
| policy\_engine\_id | The policy engine attached to the gateway (facts.sample\_tools.policy\_engine\_id); this module adds one Cedar policy per tool to it. | `string` | n/a | yes |
| sample\_role\_arns | Execution role per sample runtime, by sample id (facts.runtime.roles.samples). The Cedar policies permit these principals. | `map(string)` | n/a | yes |
| tool\_ids | Tool packages under samples/tools/ to deploy. Each has a tool.yaml. | `list(string)` | n/a | yes |
| credential\_provider\_arns | API-key credential providers by tool id (facts.sample\_tools.credential\_provider\_arns). An OpenAPI tool is deployed only when its provider is here. | `map(string)` | `{}` | no |
| name\_suffix | Environment suffix appended to this module's names (empty in production, -staging in staging). | `string` | `""` | no |

## Outputs

| Name | Description |
| ---- | ----------- |
| deployed\_tools | Tool ids with a target on the gateway, by kind. |
| function\_names | Tool Lambda function names by tool id. |
| skipped\_tools | OpenAPI tools skipped for want of a credential provider in the facts. |
| tool\_names | MCP tool names as the gateway exposes them (<target>\_\_\_<operation>), per tool id. |
<!-- END_TF_DOCS -->
