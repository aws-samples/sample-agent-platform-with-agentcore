"""Per-agent credentials for a customer-owned MCP hub (MCPHUB-HMAC-SHA256).

A published agent that attaches an ``mcp-hub`` MCP server is an *application*
in the hub's eyes: it signs every request with an access/secret key pair (the
Actor) while the acting user's SSO token rides alongside. Publishing is the
natural minting point — each agent gets its own pair, so the hub can tell
agents apart, rate-limit them independently, and one revocation never takes
down a neighbour.

The pair lives only in Secrets Manager (``agent-platform/mcp-hub/{agent_id}``
as ``{"access_key": …, "secret_key": …}``). The platform stores and shows the
access key — it is an identifier, not a secret — but the secret key is never
returned by any API and never rides in an invocation payload: the kernel
receives the secret *name* and the signing proxy fetches the pair under the
runtime role, which is granted exactly this prefix.

Registering the pair with the hub is the hub operator's step (config or
HUB_ACTORS env there); the deploy tooling shows how to sync it.
"""

import json
import logging
import secrets

import boto3

from app.config import settings

logger = logging.getLogger(__name__)

class McpHubCredentialsService:
    def __init__(self) -> None:
        self.sm = boto3.client("secretsmanager", region_name=settings.aws_region)

    @staticmethod
    def secret_name(agent_id: str) -> str:
        return f"{settings.mcp_hub_secret_prefix}/{agent_id}"

    def ensure(self, agent_id: str) -> str:
        """Create the agent's credential pair if it does not exist yet;
        return the access key either way. Idempotent — a republish keeps the
        existing pair so the hub's actor registry stays valid."""
        name = self.secret_name(agent_id)
        try:
            existing = self.sm.get_secret_value(SecretId=name)
            return str(json.loads(existing["SecretString"]).get("access_key", ""))
        except self.sm.exceptions.ResourceNotFoundException:
            pass
        access_key = f"agent-{agent_id}"
        self.sm.create_secret(
            Name=name,
            Description=f"MCP hub HMAC credentials (Actor) for published agent {agent_id}",
            SecretString=json.dumps(
                {"access_key": access_key, "secret_key": secrets.token_urlsafe(32)}
            ),
        )
        logger.info("minted mcp-hub credentials for agent %s", agent_id)
        return access_key

    def rotate(self, agent_id: str) -> str:
        """New secret key, same access key. The hub must learn the new value
        before the next invocation — rotation is a two-step dance by design."""
        name = self.secret_name(agent_id)
        current = json.loads(self.sm.get_secret_value(SecretId=name)["SecretString"])
        current["secret_key"] = secrets.token_urlsafe(32)
        self.sm.put_secret_value(SecretId=name, SecretString=json.dumps(current))
        return str(current.get("access_key", ""))

    def delete(self, agent_id: str) -> None:
        """Drop the pair when its agent is deleted (recoverable for 7 days,
        Secrets Manager's minimum window)."""
        try:
            self.sm.delete_secret(
                SecretId=self.secret_name(agent_id), RecoveryWindowInDays=7
            )
        except self.sm.exceptions.ResourceNotFoundException:
            pass
        except Exception:
            logger.warning(
                "could not delete mcp-hub credentials for %s", agent_id, exc_info=True
            )


mcp_hub_credentials_service = McpHubCredentialsService()
