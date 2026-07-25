"""Team-demo stack: a JWT-inbound AgentCore Runtime for the SSO auth chain.

The platform's standard runtimes use IAM (SigV4) inbound auth — every portal
feature (terminal WebSocket, scheduler Lambda, channels) is built on it and
stays untouched. A runtime supports exactly one inbound authorizer, so the
end-user-identity path gets its **own** runtime: the same headless
agent-sdk-kernel image, but with a CUSTOM_JWT authorizer pointing at the
Keycloak realm from TeamAuthStack.

Callers invoke it with the end user's access token instead of SigV4; the
`team` claim minted by the IdP therefore rides along on every invocation, and
the kernel forwards the same token as the MCP bearer to the AgentCore Gateway
(created by scripts/deploy_team_gateway.py).

Deploy this stack only after TeamAuthStack is up and Keycloak answers on its
discovery URL — AgentCore validates the URL when creating the authorizer.
"""

from aws_cdk import CfnOutput, Stack
from aws_cdk import aws_bedrockagentcore as agentcore
from constructs import Construct

from stacks.network_stack import NetworkStack
from stacks.platform_stack import PlatformStack
from stacks.runtime_stack import RuntimeStack
from stacks.team_auth_stack import TeamAuthStack


class TeamDemoStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        network: NetworkStack,
        platform: PlatformStack,
        runtime: RuntimeStack,
        team_auth: TeamAuthStack,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        ctx = self.node.try_get_context
        # own tag knob: the demo kernel usually runs a NEWER build than the
        # pinned production runtimes (it needs mcp_servers[].headers support)
        image_tag = ctx("team_demo_image_tag") or ctx("image_tag") or "latest"
        llm_gateway_url = ctx("llm_gateway_url") or ""
        use_bedrock = str(ctx("use_bedrock") or ("" if llm_gateway_url else "1"))
        anthropic_model = ctx("anthropic_model") or ""
        anthropic_small_model = ctx("anthropic_small_fast_model") or ""

        env: dict[str, str] = {
            "AWS_REGION": self.region,
            "LLM_GATEWAY_SECRET_NAME": platform.llm_gateway_secret.secret_name,
            # bump to force a new runtime version when the image tag is
            # mutable (AgentCore resolves the tag at version creation)
            "KERNEL_BUILD": self.node.try_get_context("team_demo_build") or "1",
        }
        if llm_gateway_url:
            env["ANTHROPIC_BASE_URL"] = llm_gateway_url
        if use_bedrock == "1":
            env["CLAUDE_CODE_USE_BEDROCK"] = "1"
        if anthropic_model:
            env["ANTHROPIC_MODEL"] = anthropic_model
        if anthropic_small_model:
            env["ANTHROPIC_SMALL_FAST_MODEL"] = anthropic_small_model

        demo = agentcore.CfnRuntime(
            self,
            "TeamDemoRuntime",
            agent_runtime_name="team_demo_kernel",
            description=(
                "Headless kernel with OAuth (JWT) inbound auth - invoked with the "
                "end user's Keycloak token so the team claim propagates end to end"
            ),
            agent_runtime_artifact=agentcore.CfnRuntime.AgentRuntimeArtifactProperty(
                container_configuration=agentcore.CfnRuntime.ContainerConfigurationProperty(
                    container_uri=(
                        f"{platform.kernel_repos['agent-sdk-kernel'].repository_uri}:{image_tag}"
                    )
                )
            ),
            # same execution role as the other kernels: identical image,
            # identical AWS needs (logs, workspace bucket, LLM gateway secret)
            role_arn=runtime.execution_role.role_arn,
            network_configuration=agentcore.CfnRuntime.NetworkConfigurationProperty(
                network_mode="VPC",
                network_mode_config=agentcore.CfnRuntime.VpcConfigProperty(
                    security_groups=[network.runtime_sg.security_group_id],
                    subnets=[s.subnet_id for s in network.vpc.private_subnets],
                ),
            ),
            protocol_configuration="HTTP",
            # Inbound auth = the Keycloak realm. Tokens must carry the
            # "agent-platform" audience (added by the realm's audience mapper).
            authorizer_configuration=agentcore.CfnRuntime.AuthorizerConfigurationProperty(
                custom_jwt_authorizer=agentcore.CfnRuntime.CustomJWTAuthorizerConfigurationProperty(
                    discovery_url=team_auth.discovery_url,
                    allowed_audience=["agent-platform"],
                )
            ),
            environment_variables=env,
        )

        self.demo_runtime_arn = demo.attr_agent_runtime_arn

        CfnOutput(self, "TeamDemoRuntimeArn", value=self.demo_runtime_arn)
