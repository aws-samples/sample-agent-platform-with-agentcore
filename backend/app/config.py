"""Backend configuration — everything comes from environment variables."""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    aws_region: str = "us-east-1"

    # DynamoDB single table for sessions
    dynamo_table: str = "agent-platform"

    # AgentCore Runtime ARNs (outputs of the RuntimeStack)
    interactive_runtime_arn: str = ""
    sdk_runtime_arn: str = ""
    mcp_tools_runtime_arn: str = ""
    runtime_qualifier: str = "DEFAULT"

    # S3 bucket where kernels persist per-session workspaces
    workspace_bucket: str = ""
    workspace_prefix: str = "workspaces"

    # Cognito user pool guarding the API (production mode). When set, every
    # /api request must carry a valid Cognito ID token as a Bearer header.
    cognito_pool_id: str = ""
    cognito_client_id: str = ""

    # Optional static bearer token guarding the API when Cognito is not
    # configured. Empty = open (local development only).
    api_token: str = ""

    # CORS origins for the portal frontend
    cors_origins: str = "http://localhost:5173"

    model_config = {"env_prefix": "PLATFORM_"}


settings = Settings()
