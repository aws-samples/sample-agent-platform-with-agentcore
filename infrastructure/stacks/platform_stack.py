"""Platform stack: shared data-plane resources for the agent platform."""

from aws_cdk import CfnOutput, RemovalPolicy, Stack, SecretValue
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_ecr as ecr
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_secretsmanager as secretsmanager
from constructs import Construct


class PlatformStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Per-session workspaces (files + Claude Code state) live here
        self.workspace_bucket = s3.Bucket(
            self,
            "WorkspaceBucket",
            bucket_name=f"agent-platform-workspaces-{self.account}-{self.region}",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.RETAIN,
        )

        # Control-plane session records
        self.table = dynamodb.Table(
            self,
            "PlatformTable",
            table_name="agent-platform",
            partition_key=dynamodb.Attribute(name="PK", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="SK", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
        )

        self.kernel_repos = {
            name: ecr.Repository(
                self,
                f"Repo{name.title().replace('-', '')}",
                repository_name=f"agent-platform/{name}",
                removal_policy=RemovalPolicy.DESTROY,
                empty_on_delete=True,
            )
            for name in ["claude-code-kernel", "agent-sdk-kernel", "mcp-tools-kernel", "backend"]
        }

        # Placeholder — put your real gateway key with:
        #   aws secretsmanager put-secret-value --secret-id agent-platform/llm-gateway-key \
        #     --secret-string '{"api_key":"sk-..."}'
        self.llm_gateway_secret = secretsmanager.Secret(  # nosec B106
            self,
            "LlmGatewayKey",
            # This is the Secrets Manager secret *name* (an identifier), not a
            # credential; the real value is set out-of-band (see comment above).
            secret_name="agent-platform/llm-gateway-key",
            description="API key for the Anthropic-compatible LLM gateway (LiteLLM etc.)",
            secret_object_value={"api_key": SecretValue.unsafe_plain_text("REPLACE_ME")},
        )

        CfnOutput(self, "WorkspaceBucketName", value=self.workspace_bucket.bucket_name)
        CfnOutput(self, "TableName", value=self.table.table_name)
        for name, repo in self.kernel_repos.items():
            CfnOutput(self, f"EcrUri{name.replace('-', '')}", value=repo.repository_uri)
        CfnOutput(self, "LlmGatewaySecretName", value=self.llm_gateway_secret.secret_name)
