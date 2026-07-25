"""Team-auth stack: the external IdP (Keycloak) and three team-scoped backend
APIs, all behind one ALB + CloudFront.

This is the infrastructure for the enterprise-SSO auth chain demo:

    browser -> Keycloak login (team claim in the token)
            -> portal / AgentCore Runtime (JWT inbound auth)
            -> AgentCore Gateway (JWT inbound auth)
            -> team-a-api / team-b-api (OBO token exchange out; they validate
               the exchanged user token and enforce the team claim themselves
               — app-layer authorization)
            -> team-c-api (no SSO capability: the gateway's Lambda REQUEST
               interceptor enforces the team claim, an API-key credential
               provider opens the backend)

Layout behind one CloudFront distribution (HTTPS — required for the OIDC
discovery URL that AgentCore Runtime/Gateway authorizers fetch):

    /realms/*, /resources/*, /admin/*  -> Keycloak   (ALB default action)
    /team-a/*                          -> team-a-api (app-layer SSO authz)
    /team-b/*                          -> team-b-api (app-layer SSO authz)
    /team-c/*                          -> team-c-api (no SSO capability;
                                          static API key only — team authz is
                                          enforced by the gateway's Lambda
                                          REQUEST interceptor upstream)

Test-grade by design: Keycloak runs in dev mode with an in-memory H2 database
(realm structure re-imports from the image at boot; user passwords are
re-seeded by scripts/seed_team_idp.py after any task restart).

The AgentCore Gateway + targets are created by scripts/deploy_team_gateway.py
(the CloudFormation schema does not cover passthrough targets yet), and the
JWT-inbound demo runtime lives in TeamDemoStack — both need Keycloak's
discovery URL to be live before they can be created.
"""

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_elasticloadbalancingv2 as elbv2
from aws_cdk import aws_logs as logs
from aws_cdk import aws_secretsmanager as secretsmanager
from constructs import Construct

from stacks.network_stack import NetworkStack
from stacks.platform_stack import PlatformStack

REALM = "agent-platform"


class TeamAuthStack(Stack):
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

        image_tag = self.node.try_get_context("team_auth_image_tag") or "latest"
        vpc = network.vpc
        # created by PlatformStack so images can be pushed before this stack
        # starts its ECS services (see scripts/build-and-push-team-auth.sh)
        self.repos = platform.team_auth_repos

        # ------------------------------ secrets ---------------------------
        # Keycloak bootstrap admin password. User (alice/bob/carol) passwords
        # are generated and stored by scripts/seed_team_idp.py post-deploy.
        admin_secret = secretsmanager.Secret(
            self,
            "KeycloakAdminSecret",
            secret_name="agent-platform/keycloak-admin",  # nosec B106 - secret *name*
            description="Keycloak bootstrap admin credentials (team-auth demo IdP)",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                secret_string_template='{"username": "admin"}',
                generate_string_key="password",
                exclude_characters="\"'\\/@{}$`",
                password_length=24,
            ),
        )

        # Static key for the not-yet-SSO-adapted team-c API. The gateway's
        # API-key credential provider injects it outbound (X-Api-Key), so the
        # otherwise auth-less service is not open to direct callers. Team
        # authorization for team-c happens in the gateway's Lambda REQUEST
        # interceptor, not in this backend.
        team_c_key_secret = secretsmanager.Secret(
            self,
            "TeamCApiKeySecret",
            secret_name="agent-platform/team-c-api-key",  # nosec B106 - secret *name*
            description="Static API key for the team-c demo API (no SSO capability)",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                secret_string_template="{}",
                generate_string_key="api_key",
                exclude_characters="\"'\\/@{}$`",
                password_length=40,
            ),
        )

        # ------------------------------- ECS -------------------------------
        cluster = ecs.Cluster(
            self, "Cluster", vpc=vpc, cluster_name="agent-platform-team-auth"
        )

        alb_sg = ec2.SecurityGroup(
            self,
            "AlbSg",
            vpc=vpc,
            description="Team-auth ALB - CloudFront origin-facing traffic only",
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
        service_sg.add_ingress_rule(alb_sg, ec2.Port.tcp(8080), "keycloak from ALB")
        service_sg.add_ingress_rule(alb_sg, ec2.Port.tcp(8000), "team APIs from ALB")

        alb = elbv2.ApplicationLoadBalancer(
            self,
            "Alb",
            vpc=vpc,
            internet_facing=True,
            security_group=alb_sg,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
        )

        keycloak_tg = elbv2.ApplicationTargetGroup(
            self,
            "KeycloakTg",
            vpc=vpc,
            port=8080,
            protocol=elbv2.ApplicationProtocol.HTTP,
            target_type=elbv2.TargetType.IP,
            health_check=elbv2.HealthCheck(
                # master realm endpoint answers 200 once Keycloak (and the
                # realm import that precedes listening) is up
                path="/realms/master",
                interval=Duration.seconds(30),
                healthy_threshold_count=2,
                unhealthy_threshold_count=5,
            ),
            deregistration_delay=Duration.seconds(30),
        )
        listener = alb.add_listener(
            "Http", port=80, default_target_groups=[keycloak_tg]
        )

        team_tgs: dict[str, elbv2.ApplicationTargetGroup] = {}
        for i, team in enumerate(("team-a", "team-b", "team-c")):
            tg = elbv2.ApplicationTargetGroup(
                self,
                f"Tg-{team}",
                vpc=vpc,
                port=8000,
                protocol=elbv2.ApplicationProtocol.HTTP,
                target_type=elbv2.TargetType.IP,
                health_check=elbv2.HealthCheck(
                    path=f"/{team}/health",
                    interval=Duration.seconds(30),
                    healthy_threshold_count=2,
                    unhealthy_threshold_count=3,
                ),
                deregistration_delay=Duration.seconds(30),
            )
            listener.add_action(
                f"Route-{team}",
                priority=10 + i,
                conditions=[elbv2.ListenerCondition.path_patterns([f"/{team}/*"])],
                action=elbv2.ListenerAction.forward([tg]),
            )
            team_tgs[team] = tg

        # ---------------------------- CloudFront --------------------------
        # HTTPS front for everything. The custom X-Forwarded-Proto header is
        # the standard fix for Keycloak behind an HTTP-only ALB origin: the
        # ALB forwards origin custom headers untouched, so Keycloak (with
        # KC_PROXY_HEADERS=xforwarded) sees the true edge protocol.
        api_origin = origins.LoadBalancerV2Origin(
            alb,
            protocol_policy=cloudfront.OriginProtocolPolicy.HTTP_ONLY,
            read_timeout=Duration.seconds(60),
            custom_headers={"X-Forwarded-Proto": "https"},
        )
        distribution = cloudfront.Distribution(
            self,
            "Distribution",
            comment="agent-platform team-auth (Keycloak IdP + team APIs)",
            default_behavior=cloudfront.BehaviorOptions(
                origin=api_origin,
                allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
                cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
                origin_request_policy=cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
            ),
        )
        base_url = f"https://{distribution.distribution_domain_name}"
        self.issuer_url = f"{base_url}/realms/{REALM}"
        self.discovery_url = f"{self.issuer_url}/.well-known/openid-configuration"
        self.base_url = base_url

        # ----------------------------- Keycloak ---------------------------
        kc_logs = logs.LogGroup(
            self,
            "KeycloakLogs",
            log_group_name="/ecs/agent-platform-keycloak",
            retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=RemovalPolicy.DESTROY,
        )
        kc_task = ecs.FargateTaskDefinition(
            self,
            "KeycloakTask",
            cpu=1024,
            memory_limit_mib=2048,
            runtime_platform=ecs.RuntimePlatform(
                operating_system_family=ecs.OperatingSystemFamily.LINUX,
                cpu_architecture=ecs.CpuArchitecture.ARM64,
            ),
        )
        kc_container = kc_task.add_container(
            "keycloak",
            image=ecs.ContainerImage.from_ecr_repository(
                self.repos["keycloak"], tag=image_tag
            ),
            logging=ecs.LogDriver.aws_logs(stream_prefix="keycloak", log_group=kc_logs),
            environment={
                "KC_BOOTSTRAP_ADMIN_USERNAME": "admin",
                "KC_HOSTNAME": base_url,
                "KC_HTTP_ENABLED": "true",
                "KC_PROXY_HEADERS": "xforwarded",
                "KC_HEALTH_ENABLED": "true",
            },
            secrets={
                "KC_BOOTSTRAP_ADMIN_PASSWORD": ecs.Secret.from_secrets_manager(
                    admin_secret, "password"
                ),
            },
        )
        kc_container.add_port_mappings(ecs.PortMapping(container_port=8080))
        kc_service = ecs.FargateService(
            self,
            "KeycloakService",
            cluster=cluster,
            task_definition=kc_task,
            desired_count=1,
            security_groups=[service_sg],
            vpc_subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS
            ),
            health_check_grace_period=Duration.seconds(180),
        )
        kc_service.attach_to_application_target_group(keycloak_tg)

        # ---------------------------- team APIs ---------------------------
        # team-a / team-b validate caller tokens against the IdP themselves
        # (app-layer SSO authz). team-c models a new, not-yet-SSO-adapted API:
        # it only checks a static key, and relies on the gateway's Lambda
        # interceptor for team authorization.
        for team in ("team-a", "team-b", "team-c"):
            api_logs = logs.LogGroup(
                self,
                f"ApiLogs-{team}",
                log_group_name=f"/ecs/agent-platform-{team}-api",
                retention=logs.RetentionDays.ONE_WEEK,
                removal_policy=RemovalPolicy.DESTROY,
            )
            task = ecs.FargateTaskDefinition(
                self,
                f"ApiTask-{team}",
                cpu=256,
                memory_limit_mib=512,
                runtime_platform=ecs.RuntimePlatform(
                    operating_system_family=ecs.OperatingSystemFamily.LINUX,
                    cpu_architecture=ecs.CpuArchitecture.ARM64,
                ),
            )
            if team == "team-c":
                environment = {"TEAM": team, "TEAM_API_AUTH": "api-key"}
                secrets = {
                    "TEAM_API_KEY": ecs.Secret.from_secrets_manager(
                        team_c_key_secret, "api_key"
                    ),
                }
            else:
                environment = {
                    "TEAM": team,
                    "OIDC_ISSUER": self.issuer_url,
                    "OIDC_AUDIENCE": "agent-platform",
                }
                secrets = {}
            container = task.add_container(
                "api",
                image=ecs.ContainerImage.from_ecr_repository(
                    self.repos["team-api"], tag=image_tag
                ),
                logging=ecs.LogDriver.aws_logs(
                    stream_prefix=team, log_group=api_logs
                ),
                environment=environment,
                secrets=secrets,
            )
            container.add_port_mappings(ecs.PortMapping(container_port=8000))
            service = ecs.FargateService(
                self,
                f"ApiService-{team}",
                cluster=cluster,
                task_definition=task,
                desired_count=1,
                security_groups=[service_sg],
                vpc_subnets=ec2.SubnetSelection(
                    subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS
                ),
            )
            service.attach_to_application_target_group(team_tgs[team])

        CfnOutput(self, "TeamAuthUrl", value=base_url)
        CfnOutput(self, "KeycloakIssuer", value=self.issuer_url)
        CfnOutput(self, "KeycloakDiscoveryUrl", value=self.discovery_url)
        CfnOutput(self, "KeycloakAdminSecretArn", value=admin_secret.secret_arn)
        CfnOutput(self, "TeamAApiMcpUrl", value=f"{base_url}/team-a/mcp")
        CfnOutput(self, "TeamBApiMcpUrl", value=f"{base_url}/team-b/mcp")
        CfnOutput(self, "TeamCApiMcpUrl", value=f"{base_url}/team-c/mcp")
        CfnOutput(self, "TeamCApiKeySecretArn", value=team_c_key_secret.secret_arn)
        CfnOutput(self, "AlbDnsName", value=alb.load_balancer_dns_name)
