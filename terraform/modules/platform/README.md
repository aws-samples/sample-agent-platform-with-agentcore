# platform

Port of PlatformStack: shared data-plane resources.

The full rationale is in the comments of `main.tf` and the module's other files.

<!-- BEGIN_TF_DOCS -->
## Inputs

| Name | Description | Type | Default | Required |
| ---- | ----------- | ---- | ------- | :------: |
| name\_suffix | Environment suffix appended to the bucket, table, repository, log destination and secret names. Lowercase alphanumerics and hyphens, since it ends up in S3 bucket names; empty for production. | `string` | `""` | no |
| protect\_stateful | Deletion protection on the control-plane table, and no force-delete of the image repos or the access-log bucket while they hold data. | `bool` | `true` | no |

## Outputs

| Name | Description |
| ---- | ----------- |
| cf\_log\_destination\_arn | ARN of the us-east-1 CloudWatch Logs delivery destination (the log bucket) that the portal and team\_auth distributions attach their standard-logging deliveries to. |
| kernel\_repos | ECR repositories by name (claude-code-kernel, agent-sdk-kernel, mcp-tools-kernel, backend) as {url, arn}. The kernel execution roles are granted pull on every repo in this map. |
| llm\_edge\_repo | URL and ARN of the llm-edge ECR repository. Kept out of kernel\_repos so that no kernel role can pull the image of the service holding the gateway key. |
| llm\_gateway\_secret | Name and ARN of the Secrets Manager secret for the upstream LLM gateway API key. Terraform writes a placeholder value; the real key is put out of band. |
| log\_bucket | Name and ARN of the access-log bucket for the ALBs and CloudFront distributions. Waits on the bucket policy, which log delivery is validated against. |
| platform\_table | Name and ARN of the shared control-plane DynamoDB table (PK/SK keys). Published by name in the foundation facts. |
| sandbox\_kernel\_repo | URL and ARN of the one-shot code sandbox ECR repository. Kept out of kernel\_repos so that kernel execution roles cannot pull the sandbox image. |
| team\_auth\_repos | ECR repositories for the team\_auth images (keycloak, team-api) as {url, arn}, by name. |
| workspace\_bucket | Name and ARN of the per-session workspace bucket (files and Claude Code state; skills, async outputs and the MCP hub source zip live under their own prefixes). |
<!-- END_TF_DOCS -->
