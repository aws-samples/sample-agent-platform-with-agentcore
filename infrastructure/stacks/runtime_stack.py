"""Runtime stack: the two AgentCore Runtimes (interactive + headless).

Runtimes are declared through the CloudFormation resource
``AWS::BedrockAgentCore::Runtime`` (L1). CloudFormation is the reliable
creation path in fresh accounts and turns environment-variable changes into
clean runtime version rollouts.

Both runtimes run in VPC mode so all egress leaves via the NetworkStack's
fixed-EIP NAT Gateway (see docs/architecture.md — Networking).
"""

from aws_cdk import CfnOutput, Stack
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_iam as iam
from aws_cdk import aws_bedrockagentcore as agentcore  # L1 (Cfn*) constructs
from constructs import Construct

from stacks.network_stack import NetworkStack
from stacks.platform_stack import PlatformStack


class RuntimeStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        network: NetworkStack,
        platform: PlatformStack,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # -------- configuration (cdk.json context or -c overrides) --------
        ctx = self.node.try_get_context
        image_tag = ctx("image_tag") or "latest"
        # each kernel can pin its own build (they evolve independently);
        # image_tag remains the shared default
        kernel_tag = {
            "claude-code-kernel": ctx("claude_code_image_tag") or image_tag,
            "agent-sdk-kernel": ctx("sdk_image_tag") or image_tag,
            "mcp-tools-kernel": ctx("mcp_tools_image_tag") or image_tag,
        }
        llm_gateway_url = ctx("llm_gateway_url") or ""
        use_bedrock = str(ctx("use_bedrock") or ("" if llm_gateway_url else "1"))
        anthropic_model = ctx("anthropic_model") or ""
        anthropic_small_model = ctx("anthropic_small_fast_model") or ""

        # ------------------------- execution role -------------------------
        role = iam.Role(
            self,
            "RuntimeExecutionRole",
            role_name="agent-platform-runtime-role",
            assumed_by=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
            description="Execution role for agent-platform AgentCore runtimes",
        )
        for repo in platform.kernel_repos.values():
            repo.grant_pull(role)
        role.add_to_policy(
            iam.PolicyStatement(
                sid="EcrAuth", actions=["ecr:GetAuthorizationToken"], resources=["*"]
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="Logs",
                actions=[
                    "logs:CreateLogGroup",
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                    "logs:DescribeLogGroups",
                    "logs:DescribeLogStreams",
                ],
                resources=[
                    f"arn:aws:logs:{self.region}:{self.account}:log-group:/aws/bedrock-agentcore/*"
                ],
            )
        )
        # Session workspaces
        platform.workspace_bucket.grant_read_write(role)
        # LLM gateway key
        platform.llm_gateway_secret.grant_read(role)
        # Remote-MCP credentials: url-kind registry targets may carry
        # {{secret:…}} placeholders the kernel resolves at session start
        # (e.g. the Exa API key) — grant only the platform's MCP secrets.
        role.add_to_policy(
            iam.PolicyStatement(
                sid="McpSecrets",
                actions=["secretsmanager:GetSecretValue"],
                resources=[
                    f"arn:aws:secretsmanager:{self.region}:{self.account}:secret:agent-platform/exa-api-key*"
                ],
            )
        )
        if use_bedrock == "1":
            role.add_to_policy(
                iam.PolicyStatement(
                    sid="BedrockInvoke",
                    actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                    resources=["*"],  # cross-region inference profiles span regions
                )
            )

        # --------------------------- networking ---------------------------
        vpc_config = agentcore.CfnRuntime.VpcConfigProperty(
            security_groups=[network.runtime_sg.security_group_id],
            subnets=[s.subnet_id for s in network.vpc.private_subnets],
        )
        network_configuration = agentcore.CfnRuntime.NetworkConfigurationProperty(
            network_mode="VPC", network_mode_config=vpc_config
        )

        # ----------------------- shared environment -----------------------
        common_env: dict[str, str] = {
            "AWS_REGION": self.region,
            "LLM_GATEWAY_SECRET_NAME": platform.llm_gateway_secret.secret_name,
        }
        if llm_gateway_url:
            common_env["ANTHROPIC_BASE_URL"] = llm_gateway_url
        if use_bedrock == "1":
            common_env["CLAUDE_CODE_USE_BEDROCK"] = "1"
        if anthropic_model:
            common_env["ANTHROPIC_MODEL"] = anthropic_model
        if anthropic_small_model:
            common_env["ANTHROPIC_SMALL_FAST_MODEL"] = anthropic_small_model

        # ------------------------ interactive kernel ----------------------
        interactive = agentcore.CfnRuntime(
            self,
            "InteractiveRuntime",
            agent_runtime_name="claude_code_kernel",
            description="Interactive Claude Code kernel with browser web terminal",
            agent_runtime_artifact=agentcore.CfnRuntime.AgentRuntimeArtifactProperty(
                container_configuration=agentcore.CfnRuntime.ContainerConfigurationProperty(
                    container_uri=f"{platform.kernel_repos['claude-code-kernel'].repository_uri}:{kernel_tag['claude-code-kernel']}"
                )
            ),
            role_arn=role.role_arn,
            network_configuration=network_configuration,
            protocol_configuration="HTTP",
            environment_variables={
                **common_env,
                "WORKSPACE_S3_BUCKET": platform.workspace_bucket.bucket_name,
                "WORKSPACE_S3_PREFIX": "workspaces",
            },
        )

        # ------------------------- headless kernel ------------------------
        sdk = agentcore.CfnRuntime(
            self,
            "SdkRuntime",
            agent_runtime_name="agent_sdk_kernel",
            description="Headless Claude Agent SDK kernel behind the /invocations contract",
            agent_runtime_artifact=agentcore.CfnRuntime.AgentRuntimeArtifactProperty(
                container_configuration=agentcore.CfnRuntime.ContainerConfigurationProperty(
                    container_uri=f"{platform.kernel_repos['agent-sdk-kernel'].repository_uri}:{kernel_tag['agent-sdk-kernel']}"
                )
            ),
            role_arn=role.role_arn,
            network_configuration=network_configuration,
            protocol_configuration="HTTP",
            environment_variables=common_env,
        )

        # ---------------------- MCP tools server ---------------------------
        # protocol MCP: AgentCore routes traffic to 0.0.0.0:8000/mcp inside
        # the container (stateless streamable-HTTP MCP contract).
        mcp_tools = agentcore.CfnRuntime(
            self,
            "McpToolsRuntime",
            agent_runtime_name="mcp_tools_kernel",
            description="Demo MCP server (mock internal tools) hosted on AgentCore",
            agent_runtime_artifact=agentcore.CfnRuntime.AgentRuntimeArtifactProperty(
                container_configuration=agentcore.CfnRuntime.ContainerConfigurationProperty(
                    container_uri=f"{platform.kernel_repos['mcp-tools-kernel'].repository_uri}:{kernel_tag['mcp-tools-kernel']}"
                )
            ),
            role_arn=role.role_arn,
            network_configuration=network_configuration,
            protocol_configuration="MCP",
            environment_variables={"AWS_REGION": self.region},
        )

        # Agent kernels reach AgentCore-hosted MCP servers through
        # mcp-proxy-for-aws, which signs requests with the container's role.
        role.add_to_policy(
            iam.PolicyStatement(
                sid="InvokeMcpRuntimes",
                actions=["bedrock-agentcore:InvokeAgentRuntime"],
                resources=[
                    f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:runtime/mcp_tools_kernel-*",
                    f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:runtime/mcp_tools_kernel-*/runtime-endpoint/*",
                ],
            )
        )

        # AgentCore built-in tools (Code Interpreter + Browser). The built-in
        # resources live in the "aws" account namespace (aws.codeinterpreter.v1
        # / aws.browser.v1); account-scoped wildcards cover custom variants.
        role.add_to_policy(
            iam.PolicyStatement(
                sid="BuiltinTools",
                actions=[
                    "bedrock-agentcore:StartCodeInterpreterSession",
                    "bedrock-agentcore:InvokeCodeInterpreter",
                    "bedrock-agentcore:StopCodeInterpreterSession",
                    "bedrock-agentcore:GetCodeInterpreterSession",
                    "bedrock-agentcore:StartBrowserSession",
                    "bedrock-agentcore:StopBrowserSession",
                    "bedrock-agentcore:GetBrowserSession",
                    "bedrock-agentcore:UpdateBrowserStream",
                    "bedrock-agentcore:ConnectBrowserAutomationStream",
                    "bedrock-agentcore:ConnectBrowserLiveViewStream",
                ],
                resources=[
                    f"arn:aws:bedrock-agentcore:{self.region}:aws:code-interpreter/aws.codeinterpreter.v1",
                    f"arn:aws:bedrock-agentcore:{self.region}:aws:browser/aws.browser.v1",
                    f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:code-interpreter/*",
                    f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:browser/*",
                ],
            )
        )

        # AgentCore Memory (data plane): kernels bound to a memory store
        # retrieve long-term records before a run and append the exchange as
        # an event after it.
        role.add_to_policy(
            iam.PolicyStatement(
                sid="MemoryData",
                actions=[
                    "bedrock-agentcore:CreateEvent",
                    "bedrock-agentcore:GetEvent",
                    "bedrock-agentcore:ListEvents",
                    "bedrock-agentcore:ListActors",
                    "bedrock-agentcore:ListSessions",
                    "bedrock-agentcore:GetMemoryRecord",
                    "bedrock-agentcore:ListMemoryRecords",
                    "bedrock-agentcore:RetrieveMemoryRecords",
                ],
                resources=[
                    f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:memory/*"
                ],
            )
        )

        self.interactive_runtime_arn = interactive.attr_agent_runtime_arn
        self.sdk_runtime_arn = sdk.attr_agent_runtime_arn
        self.mcp_tools_runtime_arn = mcp_tools.attr_agent_runtime_arn
        # exposed for TeamDemoStack: its JWT-inbound kernel runs the same
        # image with the same AWS needs, so it shares this role
        self.execution_role = role

        CfnOutput(self, "InteractiveRuntimeArn", value=self.interactive_runtime_arn)
        CfnOutput(self, "SdkRuntimeArn", value=self.sdk_runtime_arn)
        CfnOutput(self, "McpToolsRuntimeArn", value=self.mcp_tools_runtime_arn)
