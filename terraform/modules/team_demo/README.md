# team_demo

Port of TeamDemoStack: a JWT-inbound AgentCore Runtime for the SSO auth chain. Same headless agent-sdk-kernel image, but with a CUSTOM_JWT authorizer pointing at the Keycloak realm from the team_auth module.

The full rationale is in the comments of `main.tf` and the module's other files.

The runtime is invoked by end users directly (any portal login token passes its authorizer), so it always runs the kernel with `KERNEL_INBOUND=jwt`: the kernel ignores memory, skills, async, model routing and secret placeholders in the payload and attaches MCP servers only on `gateway_hosts` (AWS Continuum C004). Set `team_demo_gateway_hosts` at the root to the host of `mcp_url` in SSM `/agent-platform/team-gateway`; without it the demo runs without its team tools. Needs an agent-sdk-kernel image that knows `KERNEL_INBOUND` (app repo, C004 fix); an older image ignores it.

<!-- BEGIN_TF_DOCS -->
## Inputs

| Name | Description | Type | Default | Required |
| ---- | ----------- | ---- | ------- | :------: |
| discovery\_url | Keycloak realm OIDC discovery URL (must be live — AgentCore validates it at create time) | `string` | n/a | yes |
| execution\_role\_arn | The runtime module's SDK role — identical image, identical AWS needs | `string` | n/a | yes |
| image\_tag | Tag of the agent-sdk-kernel image to run (the root's team\_demo\_image\_tag, falling back to image\_tag). | `string` | n/a | yes |
| kernel\_repos | ECR repositories from the platform module, by name; the runtime runs the agent-sdk-kernel image. | <pre>map(object({<br/>    url = string<br/>    arn = string<br/>  }))</pre> | n/a | yes |
| model\_env | Model settings shared with the headless kernel. use\_bedrock, anthropic\_model and anthropic\_small\_fast\_model become environment variables (empty strings are left unset); the default opus/sonnet/haiku fields are not read here. | <pre>object({<br/>    use_bedrock                    = bool<br/>    anthropic_model                = string<br/>    anthropic_small_fast_model     = string<br/>    anthropic_default_opus_model   = string<br/>    anthropic_default_sonnet_model = string<br/>    anthropic_default_haiku_model  = string<br/>  })</pre> | n/a | yes |
| private\_subnet\_ids | Subnets for the team-demo runtime's VPC-mode ENIs. | `list(string)` | n/a | yes |
| runtime\_sg\_id | Security group on the team-demo runtime's ENIs (the network module's egress-only runtime group). | `string` | n/a | yes |
| gateway\_hosts | Hostnames of the AgentCore Gateways the team-demo agent may attach as MCP servers (KERNEL\_JWT\_GATEWAY\_HOSTS), e.g. the team gateway written to SSM /agent-platform/team-gateway by scripts/deploy\_team\_gateway.py. Bare hostnames, no scheme or path. Empty = no MCP server is attached. | `list(string)` | `[]` | no |
| runtime\_name\_suffix | Suffix for the runtime name (team\_demo\_kernel<suffix>). Letters, digits and underscores only, since AgentCore runtime names allow nothing else. | `string` | `""` | no |
| team\_demo\_build | Value of KERNEL\_BUILD in the runtime environment; bump it to force a new runtime version when the image tag is mutable. | `string` | `"1"` | no |

## Outputs

| Name | Description |
| ---- | ----------- |
| runtime\_arn | ARN of the JWT-inbound team-demo AgentCore runtime; surfaced as the root team\_demo\_runtime\_arn output. |
<!-- END_TF_DOCS -->
