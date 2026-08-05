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
        # In-terminal `/model opus|sonnet|haiku` aliases resolve to Anthropic
        # API IDs unless steered — on Bedrock those must be inference-profile
        # IDs (use `global.` prefixes) or the switch fails with a 400.
        alias_models = {
            "ANTHROPIC_DEFAULT_OPUS_MODEL": ctx("anthropic_default_opus_model") or "",
            "ANTHROPIC_DEFAULT_SONNET_MODEL": ctx("anthropic_default_sonnet_model")
            or anthropic_model,
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": ctx("anthropic_default_haiku_model")
            or anthropic_small_model,
        }

        # ------------------------- execution roles ------------------------
        # One role per kernel, each holding only what that kernel's code
        # actually calls (docs/permissions.md §2). The split is what makes the
        # S3 story reviewable:
        #   - interactive: skills/* read only. Workspace sync does NOT use
        #     this role — the backend mints per-session STS credentials
        #     scoped to workspaces/{sessionId}/* (see WorkspaceAccessRole
        #     below), so code inside one session cannot read another
        #     session's prefix even with the container's own credentials.
        #   - sdk (headless): skills/* read + the async-artifact prefix
        #     (feeds/*) — no workspace access at all. Add your own pipelines'
        #     output prefixes to the AsyncArtifacts grant below.
        #   - mcp-tools: no S3.
        bucket = platform.workspace_bucket

        def kernel_role(rid: str, name: str, description: str) -> iam.Role:
            r = iam.Role(
                self,
                rid,
                role_name=name,
                assumed_by=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
                description=description,
            )
            for repo in platform.kernel_repos.values():
                repo.grant_pull(r)
            r.add_to_policy(
                iam.PolicyStatement(
                    sid="EcrAuth", actions=["ecr:GetAuthorizationToken"], resources=["*"]
                )
            )
            r.add_to_policy(
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
            return r

        def grant_prefix(
            role_: iam.Role, sid: str, prefixes: list[str], *, write: bool = False
        ) -> None:
            """Object actions on the given key prefixes + a prefix-conditioned
            ListBucket (aws s3 sync / list_objects_v2 both need the list)."""
            actions = ["s3:GetObject"]
            if write:
                actions += ["s3:PutObject", "s3:AbortMultipartUpload"]
            role_.add_to_policy(
                iam.PolicyStatement(
                    sid=sid,
                    actions=actions,
                    resources=[bucket.arn_for_objects(f"{p}/*") for p in prefixes],
                )
            )
            role_.add_to_policy(
                iam.PolicyStatement(
                    sid=f"{sid}List",
                    actions=["s3:ListBucket"],
                    resources=[bucket.bucket_arn],
                    conditions={
                        "StringLike": {
                            "s3:prefix": [f"{p}/*" for p in prefixes]
                            + [f"{p}/" for p in prefixes]
                        }
                    },
                )
            )

        # MIGRATION (remove after AgentPlatformTeamDemo re-points to the SDK
        # role): the deployed TeamDemo stack imports this legacy role's ARN,
        # and CloudFormation refuses to delete an in-use export. Keep the
        # resource + export for one deploy cycle — but with the NEW narrow
        # grants (identical to the SDK role below, added further down), not
        # the old bucket-wide ones: TeamDemo's kernel is the SDK image, so
        # the tightening applies to it the moment this deploys.
        legacy_role = kernel_role(
            "RuntimeExecutionRole",
            "agent-platform-runtime-role",
            "LEGACY shared runtime role, kept only until TeamDemo re-points; "
            "grants already narrowed to the SDK-role set",
        )
        self.export_value(legacy_role.role_arn)

        interactive_role = kernel_role(
            "InteractiveExecutionRole",
            "agent-platform-interactive-role",
            "claude-code-kernel (Dev Workbench): skills read; workspace sync "
            "uses backend-minted per-session credentials, not this role",
        )
        sdk_role = kernel_role(
            "SdkExecutionRole",
            "agent-platform-sdk-role",
            "agent-sdk-kernel (headless): skills read + async artifact "
            "prefixes; no access to session workspaces",
        )
        mcp_role = kernel_role(
            "McpToolsExecutionRole",
            "agent-platform-mcp-tools-role",
            "mcp-tools-kernel: demo MCP server, no S3 access",
        )

        # Skill packages (read-only) — both agent kernels mount them.
        grant_prefix(interactive_role, "Skills", ["skills"])
        for r in (sdk_role, legacy_role):
            grant_prefix(r, "Skills", ["skills"])
        # Async task outputs + pipeline feed artifacts — headless kernel only.
        # (Keys come from the platform's pipeline layer; see
        # invocation_service.invoke_async_and_wait and pipelines/*.mjs.)
        for r in (sdk_role, legacy_role):
            grant_prefix(r, "AsyncArtifacts", ["feeds"], write=True)

        # LLM gateway key — read at container start by both agent kernels.
        platform.llm_gateway_secret.grant_read(interactive_role)
        platform.llm_gateway_secret.grant_read(sdk_role)
        platform.llm_gateway_secret.grant_read(legacy_role)
        # Remote-MCP credentials: url-kind registry targets may carry
        # {{secret:…}} placeholders resolved at session start. Only the SDK
        # kernel implements the placeholder (runtimes/agent-sdk-kernel).
        #
        # Nothing the platform ships needs this — search runs on the managed
        # Web Search connector, which authenticates with the kernel's own role
        # and has no API key. The grant stays so that registering a third-party
        # MCP server that *does* need a key works out of the box: store it as
        # agent-platform/remote-mcp-key and reference it from the target.
        for _r in (sdk_role, legacy_role):
            _r.add_to_policy(
                iam.PolicyStatement(
                    sid="McpSecrets",
                    actions=["secretsmanager:GetSecretValue"],
                    resources=[
                        f"arn:aws:secretsmanager:{self.region}:{self.account}:secret:agent-platform/remote-mcp-key*"
                    ],
                )
            )
        # Unconditional: the model control plane (Governance → Model backends)
        # can route any agent to Bedrock per invocation, regardless of which
        # backend this deployment defaults to.
        for r in (interactive_role, sdk_role, legacy_role):
            r.add_to_policy(
                iam.PolicyStatement(
                    sid="BedrockInvoke",
                    actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                    resources=["*"],  # cross-region inference profiles span regions
                )
            )

        # ---------------- per-session workspace access role ----------------
        # Holds the ONLY path to workspaces/*. The backend assumes it per
        # session with an inline session policy narrowing to
        # workspaces/{runtimeSessionId}/* and hands the resulting 1h
        # credentials to that session's container in the warmup payload
        # (role chaining caps the duration at 1h; the container refreshes
        # through a token-authenticated backend endpoint). Trust is the
        # account (standard assume-role pattern: callers still need an
        # explicit sts:AssumeRole grant — PortalStack gives it to the
        # backend task role only).
        workspace_access_role = iam.Role(
            self,
            "WorkspaceAccessRole",
            role_name="agent-platform-workspace-access",
            assumed_by=iam.AccountPrincipal(self.account),
            description="Assumed per session by the backend; session policy "
            "narrows to that session's workspaces/ prefix",
        )
        grant_prefix(workspace_access_role, "Workspaces", ["workspaces"], write=True)

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
        if use_bedrock == "1":
            for k, v in alias_models.items():
                if v:
                    common_env[k] = v

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
            role_arn=interactive_role.role_arn,
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
            role_arn=sdk_role.role_arn,
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
            role_arn=mcp_role.role_arn,
            network_configuration=network_configuration,
            protocol_configuration="MCP",
            environment_variables={"AWS_REGION": self.region},
        )

        # Agent kernels reach AgentCore-hosted MCP servers through
        # mcp-proxy-for-aws, which signs requests with the container's role.
        for r in (interactive_role, sdk_role, legacy_role):
            r.add_to_policy(
                iam.PolicyStatement(
                    sid="InvokeMcpRuntimes",
                    actions=["bedrock-agentcore:InvokeAgentRuntime"],
                    resources=[
                        f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:runtime/mcp_tools_kernel-*",
                        f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:runtime/mcp_tools_kernel-*/runtime-endpoint/*",
                    ],
                )
            )

        # AgentCore Gateways reached as MCP servers (registry kind
        # "agentcore-gateway") — same SigV4 proxy as a runtime. The feed
        # pipelines search through one of these: the managed Web Search
        # connector behind scripts/deploy_websearch_gateway.py.
        #
        # Gateway IDs are generated at deploy time, so this is scoped by
        # account+region rather than to one gateway. us-east-1 is listed
        # explicitly because the Web Search connector is only offered there —
        # a platform deployed elsewhere still has to reach that gateway.
        for r in (interactive_role, sdk_role, legacy_role):
            r.add_to_policy(
                iam.PolicyStatement(
                    sid="InvokeGateways",
                    actions=["bedrock-agentcore:InvokeGateway"],
                    resources=sorted(
                        {
                            f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:gateway/*",
                            f"arn:aws:bedrock-agentcore:us-east-1:{self.account}:gateway/*",
                        }
                    ),
                )
            )

        # AgentCore built-in tools (Code Interpreter + Browser). The built-in
        # resources live in the "aws" account namespace (aws.codeinterpreter.v1
        # / aws.browser.v1); account-scoped wildcards cover custom variants.
        # Both agent kernels ship the builtin_tools_mcp.py wrapper.
        builtin_tools = iam.PolicyStatement(
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
        interactive_role.add_to_policy(builtin_tools)
        sdk_role.add_to_policy(builtin_tools)
        legacy_role.add_to_policy(builtin_tools)

        # AgentCore Memory (data plane): memory-bound invocations run on the
        # headless kernel only — it retrieves long-term records before a run
        # and appends the exchange as an event after it.
        for _r in (sdk_role, legacy_role):
            _r.add_to_policy(
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

        # The runtimes reference the roles by ARN, which only orders them
        # after the Role resource — NOT after its DefaultPolicy (where the ECR
        # pull grant lives). AgentCore validates image access with the new
        # role at update time, so without this the update races the policy
        # attachment and fails with "image identifier does not exist".
        for rt, r in (
            (interactive, interactive_role),
            (sdk, sdk_role),
            (mcp_tools, mcp_role),
        ):
            default_policy = r.node.try_find_child("DefaultPolicy")
            if default_policy is not None:
                rt.node.add_dependency(default_policy)

        self.interactive_runtime_arn = interactive.attr_agent_runtime_arn
        self.sdk_runtime_arn = sdk.attr_agent_runtime_arn
        self.mcp_tools_runtime_arn = mcp_tools.attr_agent_runtime_arn
        # exposed for TeamDemoStack: its JWT-inbound kernel runs the SDK image
        # with the same AWS needs, so it shares the SDK role
        self.execution_role = sdk_role
        # exposed for PortalStack: the backend assumes this per session to
        # mint workspace-scoped S3 credentials for the interactive kernel
        self.workspace_access_role = workspace_access_role

        CfnOutput(self, "InteractiveRuntimeArn", value=self.interactive_runtime_arn)
        CfnOutput(self, "SdkRuntimeArn", value=self.sdk_runtime_arn)
        CfnOutput(self, "McpToolsRuntimeArn", value=self.mcp_tools_runtime_arn)
        CfnOutput(self, "WorkspaceAccessRoleArn", value=workspace_access_role.role_arn)
