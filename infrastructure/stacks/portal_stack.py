"""Portal stack: backend on ECS Fargate + CloudFront in front of ALB and the
static frontend bucket.

Optional — for local development you can skip this stack and run
``uvicorn app.main:app`` + ``npm run dev`` against the RuntimeStack outputs.
"""

from aws_cdk import CfnOutput, Duration, Stack
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_elasticloadbalancingv2 as elbv2
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_scheduler as scheduler
from aws_cdk import aws_sqs as sqs
from aws_cdk import RemovalPolicy
from constructs import Construct

from stacks.network_stack import NetworkStack
from stacks.platform_stack import PlatformStack
from stacks.runtime_stack import RuntimeStack


class PortalStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        network: NetworkStack,
        platform: PlatformStack,
        runtime: RuntimeStack,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # backend image can rev independently of the (pinned) kernel images
        image_tag = (
            self.node.try_get_context("backend_image_tag")
            or self.node.try_get_context("image_tag")
            or "latest"
        )
        vpc = network.vpc

        # Enterprise-SSO mode (TeamAuthStack): pass the Keycloak issuer via
        # context and the backend verifies OIDC access tokens instead of
        # Cognito ID tokens (the Cognito pool below still deploys, unused).
        #   cdk deploy AgentPlatformPortal -c oidc_issuer=https://<cf>/realms/agent-platform
        oidc_issuer = self.node.try_get_context("oidc_issuer") or ""
        oidc_client_id = self.node.try_get_context("oidc_client_id") or "portal-web"
        oidc_audience = self.node.try_get_context("oidc_audience") or "agent-platform"

        # ------------------------------- auth -----------------------------
        # Cognito user pool guarding the portal. Self-signup is disabled —
        # an operator creates users (admin-create-user). The frontend signs
        # in with USER_PASSWORD_AUTH and sends the ID token as a Bearer
        # header; the backend verifies it against the pool's JWKS.
        user_pool = cognito.UserPool(
            self,
            "UserPool",
            user_pool_name="agent-platform",
            self_sign_up_enabled=False,
            sign_in_aliases=cognito.SignInAliases(email=True, username=True),
            password_policy=cognito.PasswordPolicy(min_length=12),
            removal_policy=RemovalPolicy.DESTROY,
        )
        user_pool_client = user_pool.add_client(
            "PortalClient",
            auth_flows=cognito.AuthFlow(user_password=True),
            id_token_validity=Duration.hours(12),
            access_token_validity=Duration.hours(12),
        )

        # ----------------------------- frontend ---------------------------
        frontend_bucket = s3.Bucket(
            self,
            "FrontendBucket",
            bucket_name=f"agent-platform-frontend-{self.account}-{self.region}",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )

        # ------------------------------ backend ---------------------------
        cluster = ecs.Cluster(self, "Cluster", vpc=vpc, cluster_name="agent-platform")

        log_group = logs.LogGroup(
            self,
            "BackendLogs",
            log_group_name="/ecs/agent-platform-backend",
            retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=RemovalPolicy.DESTROY,
        )

        task_role = iam.Role(
            self, "TaskRole", assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com")
        )
        platform.table.grant_read_write_data(task_role)
        # read: session artifacts browsing; write: skill packages under skills/
        platform.workspace_bucket.grant_read_write(task_role)
        # Per-session workspace credentials: the backend assumes the
        # workspace-access role with a session policy narrowing S3 to
        # workspaces/{sessionId}/* and hands the credentials to that
        # session's interactive container (warmup payload + refresh API).
        task_role.add_to_policy(
            iam.PolicyStatement(
                sid="AssumeWorkspaceAccess",
                actions=["sts:AssumeRole"],
                resources=[runtime.workspace_access_role.role_arn],
            )
        )
        task_role.add_to_policy(
            iam.PolicyStatement(
                sid="AgentCoreInvoke",
                actions=[
                    "bedrock-agentcore:InvokeAgentRuntime",
                    # Required for browsers to complete the SigV4 pre-signed
                    # WebSocket handshake to the runtime /ws endpoint.
                    "bedrock-agentcore:InvokeAgentRuntimeWithWebSocketStream",
                    "bedrock-agentcore:GetAgentRuntime",
                    "bedrock-agentcore:GetAgentRuntimeEndpoint",
                ],
                resources=[
                    f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:runtime/*"
                ],
            )
        )
        # AgentCore Memory: the Memory page manages stores (control plane) and
        # browses events/records (data plane) through the backend.
        task_role.add_to_policy(
            iam.PolicyStatement(
                sid="AgentCoreMemory",
                actions=[
                    "bedrock-agentcore:CreateMemory",
                    "bedrock-agentcore:GetMemory",
                    "bedrock-agentcore:UpdateMemory",
                    "bedrock-agentcore:DeleteMemory",
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
        # Team-auth demo: the backend reads the gateway/runtime wiring that
        # scripts/deploy_team_gateway.py stores in SSM (read-only, demo-scoped).
        task_role.add_to_policy(
            iam.PolicyStatement(
                sid="TeamDemoParam",
                actions=["ssm:GetParameter"],
                resources=[
                    f"arn:aws:ssm:{self.region}:{self.account}:parameter/agent-platform/team-gateway"
                ],
            )
        )
        task_role.add_to_policy(
            iam.PolicyStatement(
                sid="AgentCoreMemoryList",
                # ListMemories is not resource-scoped
                actions=["bedrock-agentcore:ListMemories"],
                resources=["*"],
            )
        )
        # The Gateway page reads gateway/target configuration (read-only: no
        # Create/Update/Delete). ListGateways is not resource-scoped.
        task_role.add_to_policy(
            iam.PolicyStatement(
                sid="AgentCoreGatewayRead",
                actions=[
                    "bedrock-agentcore:ListGateways",
                    "bedrock-agentcore:GetGateway",
                    "bedrock-agentcore:ListGatewayTargets",
                    "bedrock-agentcore:GetGatewayTarget",
                ],
                resources=["*"],
            )
        )
        # Pipeline runs emit an orchestration trace (root → phase → agent
        # spans) via X-Ray; with Transaction Search enabled they render in the
        # CloudWatch Traces console. PutTraceSegments is not resource-scoped.
        task_role.add_to_policy(
            iam.PolicyStatement(
                sid="PipelineTraces",
                actions=["xray:PutTraceSegments", "xray:PutTelemetryRecords"],
                resources=["*"],
            )
        )

        # --------------------------- scheduler -----------------------------
        # Production firing engine: one EventBridge Scheduler schedule per
        # platform schedule (created at runtime by the backend, in this
        # dedicated group) -> the schedule-runner Lambda -> the same governed
        # invocation pipeline. The Lambda packages the backend's service
        # layer; its code is deployed by scripts/deploy-schedule-lambda.sh
        # (mirroring how kernel images are pushed outside CloudFormation).
        schedule_group = scheduler.CfnScheduleGroup(
            self, "ScheduleGroup", name="agent-platform"
        )

        schedule_dlq = sqs.Queue(
            self,
            "ScheduleDlq",
            queue_name="agent-platform-schedule-dlq",
            enforce_ssl=True,
            retention_period=Duration.days(14),
        )

        runner_logs = logs.LogGroup(
            self,
            "ScheduleRunnerLogs",
            log_group_name="/aws/lambda/agent-platform-schedule-runner",
            retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=RemovalPolicy.DESTROY,
        )
        runner_fn = lambda_.Function(
            self,
            "ScheduleRunner",
            function_name="agent-platform-schedule-runner",
            runtime=lambda_.Runtime.PYTHON_3_13,
            architecture=lambda_.Architecture.ARM_64,
            handler="index.handler",
            # placeholder until scripts/deploy-schedule-lambda.sh uploads the
            # real package (backend `app` module + handler + dependencies)
            code=lambda_.Code.from_inline(
                "def handler(event, context):\n"
                "    raise RuntimeError('schedule-runner code not deployed - "
                "run scripts/deploy-schedule-lambda.sh')\n"
            ),
            # a single agent run can legitimately take minutes
            timeout=Duration.minutes(10),
            memory_size=512,
            log_group=runner_logs,
            environment={
                "PLATFORM_AWS_REGION": self.region,
                "PLATFORM_DYNAMO_TABLE": platform.table.table_name,
                "PLATFORM_WORKSPACE_BUCKET": platform.workspace_bucket.bucket_name,
                "PLATFORM_INTERACTIVE_RUNTIME_ARN": runtime.interactive_runtime_arn,
                "PLATFORM_SDK_RUNTIME_ARN": runtime.sdk_runtime_arn,
                "PLATFORM_MCP_TOOLS_RUNTIME_ARN": runtime.mcp_tools_runtime_arn,
            },
        )
        platform.table.grant_read_write_data(runner_fn)
        # pipeline schedules read staged feeds and write the shortlist
        platform.workspace_bucket.grant_read_write(runner_fn)
        runner_fn.add_to_role_policy(
            iam.PolicyStatement(
                sid="AgentCoreInvoke",
                actions=["bedrock-agentcore:InvokeAgentRuntime"],
                resources=[
                    f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:runtime/*"
                ],
            )
        )
        runner_fn.add_to_role_policy(
            iam.PolicyStatement(
                sid="PipelineTraces",
                actions=["xray:PutTraceSegments", "xray:PutTelemetryRecords"],
                resources=["*"],
            )
        )

        # role EventBridge Scheduler assumes to invoke the runner
        scheduler_role = iam.Role(
            self,
            "SchedulerRole",
            assumed_by=iam.PrincipalWithConditions(
                iam.ServicePrincipal("scheduler.amazonaws.com"),
                {"StringEquals": {"aws:SourceAccount": self.account}},
            ),
        )
        runner_fn.grant_invoke(scheduler_role)
        schedule_dlq.grant_send_messages(scheduler_role)

        # the backend mirrors schedule CRUD into the group
        task_role.add_to_policy(
            iam.PolicyStatement(
                sid="SchedulerCrud",
                actions=[
                    "scheduler:CreateSchedule",
                    "scheduler:UpdateSchedule",
                    "scheduler:DeleteSchedule",
                    "scheduler:GetSchedule",
                    "scheduler:ListSchedules",
                ],
                resources=[
                    f"arn:aws:scheduler:{self.region}:{self.account}:schedule/{schedule_group.name}/*"
                ],
            )
        )
        task_role.add_to_policy(
            iam.PolicyStatement(
                sid="PassSchedulerRole",
                actions=["iam:PassRole"],
                resources=[scheduler_role.role_arn],
                conditions={
                    "StringEquals": {"iam:PassedToService": "scheduler.amazonaws.com"}
                },
            )
        )

        task_def = ecs.FargateTaskDefinition(
            self,
            "TaskDef",
            cpu=512,
            memory_limit_mib=1024,
            task_role=task_role,
            runtime_platform=ecs.RuntimePlatform(
                operating_system_family=ecs.OperatingSystemFamily.LINUX,
                cpu_architecture=ecs.CpuArchitecture.ARM64,
            ),
        )
        container = task_def.add_container(
            "backend",
            image=ecs.ContainerImage.from_ecr_repository(
                platform.kernel_repos["backend"], tag=image_tag
            ),
            logging=ecs.LogDriver.aws_logs(stream_prefix="backend", log_group=log_group),
            environment={
                "PLATFORM_AWS_REGION": self.region,
                "PLATFORM_DYNAMO_TABLE": platform.table.table_name,
                "PLATFORM_WORKSPACE_BUCKET": platform.workspace_bucket.bucket_name,
                "PLATFORM_INTERACTIVE_RUNTIME_ARN": runtime.interactive_runtime_arn,
                "PLATFORM_SDK_RUNTIME_ARN": runtime.sdk_runtime_arn,
                "PLATFORM_MCP_TOOLS_RUNTIME_ARN": runtime.mcp_tools_runtime_arn,
                "PLATFORM_WORKSPACE_ACCESS_ROLE_ARN": runtime.workspace_access_role.role_arn,
                "PLATFORM_CORS_ORIGINS": "*",
                "PLATFORM_COGNITO_POOL_ID": user_pool.user_pool_id,
                "PLATFORM_COGNITO_CLIENT_ID": user_pool_client.user_pool_client_id,
                **(
                    {
                        "PLATFORM_OIDC_ISSUER": oidc_issuer,
                        "PLATFORM_OIDC_CLIENT_ID": oidc_client_id,
                        "PLATFORM_OIDC_AUDIENCE": oidc_audience,
                    }
                    if oidc_issuer
                    else {}
                ),
                # switches the scheduler into eventbridge mode
                "PLATFORM_SCHEDULER_GROUP": schedule_group.name,
                "PLATFORM_SCHEDULER_LAMBDA_ARN": runner_fn.function_arn,
                "PLATFORM_SCHEDULER_ROLE_ARN": scheduler_role.role_arn,
                "PLATFORM_SCHEDULER_DLQ_ARN": schedule_dlq.queue_arn,
            },
        )
        container.add_port_mappings(ecs.PortMapping(container_port=8000))

        # ALB only accepts traffic that came through CloudFront
        alb_sg = ec2.SecurityGroup(
            self,
            "AlbSg",
            vpc=vpc,
            description="ALB - CloudFront origin-facing traffic only",
            allow_all_outbound=True,
        )
        cloudfront_prefix = ec2.PrefixList.from_lookup(
            self,
            "CloudFrontOriginFacing",
            prefix_list_name="com.amazonaws.global.cloudfront.origin-facing",
        )
        alb_sg.add_ingress_rule(
            ec2.Peer.prefix_list(cloudfront_prefix.prefix_list_id),
            ec2.Port.tcp(80),
            "HTTP from CloudFront origin-facing ranges",
        )

        service_sg = ec2.SecurityGroup(
            self, "ServiceSg", vpc=vpc, allow_all_outbound=True
        )
        service_sg.add_ingress_rule(alb_sg, ec2.Port.tcp(8000), "from ALB")

        alb = elbv2.ApplicationLoadBalancer(
            self,
            "Alb",
            vpc=vpc,
            internet_facing=True,
            security_group=alb_sg,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
        )
        target_group = elbv2.ApplicationTargetGroup(
            self,
            "Tg",
            vpc=vpc,
            port=8000,
            protocol=elbv2.ApplicationProtocol.HTTP,
            target_type=elbv2.TargetType.IP,
            health_check=elbv2.HealthCheck(
                path="/health",
                interval=Duration.seconds(30),
                healthy_threshold_count=2,
                unhealthy_threshold_count=3,
            ),
            deregistration_delay=Duration.seconds(30),
        )
        alb.add_listener("Http", port=80, default_target_groups=[target_group])

        service = ecs.FargateService(
            self,
            "Service",
            cluster=cluster,
            task_definition=task_def,
            desired_count=1,
            security_groups=[service_sg],
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
        )
        service.attach_to_application_target_group(target_group)

        # ---------------------------- CloudFront --------------------------
        api_origin = origins.LoadBalancerV2Origin(
            alb,
            protocol_policy=cloudfront.OriginProtocolPolicy.HTTP_ONLY,
            # Debug invokes can run long (agent loop + built-in tool sessions);
            # 60s is the CloudFront maximum without a quota increase — longer
            # invocations should call the runtime directly, not via the portal.
            read_timeout=Duration.seconds(60),
        )
        api_behavior = cloudfront.BehaviorOptions(
            origin=api_origin,
            allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
            cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
            origin_request_policy=cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
            viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        )

        distribution = cloudfront.Distribution(
            self,
            "Distribution",
            default_root_object="index.html",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(frontend_bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
            ),
            additional_behaviors={
                "/api/*": api_behavior,
                "/health": api_behavior,
            },
            error_responses=[
                # SPA routing: unknown paths fall back to index.html
                cloudfront.ErrorResponse(
                    http_status=403, response_http_status=200, response_page_path="/index.html"
                ),
                cloudfront.ErrorResponse(
                    http_status=404, response_http_status=200, response_page_path="/index.html"
                ),
            ],
        )

        # Pipeline schedules: workflow scripts need Node (backend container
        # only), so the runner Lambda delegates pipeline runs to the backend
        # API, signing in as the portal admin.
        runner_fn.add_environment(
            "PLATFORM_PORTAL_API_URL", f"https://{distribution.distribution_domain_name}"
        )
        runner_fn.add_to_role_policy(
            iam.PolicyStatement(
                sid="PortalAdminSecret",
                actions=["secretsmanager:GetSecretValue"],
                resources=[
                    f"arn:aws:secretsmanager:{self.region}:{self.account}:secret:agent-platform/portal-admin*"
                ],
            )
        )

        CfnOutput(self, "PortalUrl", value=f"https://{distribution.distribution_domain_name}")
        CfnOutput(self, "DistributionId", value=distribution.distribution_id)
        CfnOutput(self, "FrontendBucketName", value=frontend_bucket.bucket_name)
        CfnOutput(self, "AlbDnsName", value=alb.load_balancer_dns_name)
        CfnOutput(self, "UserPoolId", value=user_pool.user_pool_id)
        CfnOutput(self, "UserPoolClientId", value=user_pool_client.user_pool_client_id)
        CfnOutput(self, "ScheduleRunnerFunction", value=runner_fn.function_name)
        CfnOutput(self, "ScheduleDlqUrl", value=schedule_dlq.queue_url)
