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

The Dev Workbench (and the Debug console) is one more application in the
hub's eyes: sessions there sign with a single shared platform Actor
(``WORKBENCH_ACTOR_ID``) rather than a per-agent pair — a workbench session
has no published-agent identity, and the hub operator should not have to
register a new Actor per developer. The acting *user* still rides in the
forwarded SSO token, so per-user permissions are unaffected.
"""

import json
import logging
import secrets

import boto3

from app.config import settings

logger = logging.getLogger(__name__)

# The shared Actor identity for anything that is not a published agent:
# workbench sessions and Debug console runs. Its access key is the bare ID
# (no "agent-" prefix) so hub logs read unambiguously.
WORKBENCH_ACTOR_ID = "dev-workbench"


class McpHubCredentialsService:
    def __init__(self) -> None:
        self.sm = boto3.client("secretsmanager", region_name=settings.aws_region)

    @staticmethod
    def secret_name(actor_id: str) -> str:
        return f"{settings.mcp_hub_secret_prefix}/{actor_id}"

    def ensure(self, actor_id: str, access_key: str | None = None) -> str:
        """Create the credential pair if it does not exist yet; return the
        access key either way. Idempotent — a republish keeps the existing
        pair so the hub's actor registry stays valid."""
        name = self.secret_name(actor_id)
        try:
            existing = self.sm.get_secret_value(SecretId=name)
            return str(json.loads(existing["SecretString"]).get("access_key", ""))
        except self.sm.exceptions.ResourceNotFoundException:
            pass
        access_key = access_key or f"agent-{actor_id}"
        self.sm.create_secret(
            Name=name,
            Description=f"MCP hub HMAC credentials (Actor) for {actor_id}",
            SecretString=json.dumps(
                {"access_key": access_key, "secret_key": secrets.token_urlsafe(32)}
            ),
        )
        logger.info("minted mcp-hub credentials for %s", actor_id)
        return access_key

    def ensure_workbench(self) -> str:
        """The shared Dev Workbench / Debug console Actor (lazy-minted on the
        first hub attachment outside a published agent)."""
        return self.ensure(WORKBENCH_ACTOR_ID, access_key=WORKBENCH_ACTOR_ID)

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
