"""Per-agent identity for a customer-owned MCP hub: HMAC key pair or IAM.

Two ways an attachment can authenticate the *application* to the hub, chosen
per registry entry (``auth``):

``hmac`` — MCPHUB-HMAC-SHA256 with an access/secret key pair per agent
(everything below the next heading). ``iam`` — no key pair at all: *this
backend* mints a short-lived session of the hub *caller role*
(``settings.mcp_hub_caller_role_arn``, from the foundation's IAM entry) named
after the actor and hands it to the kernel in the warmup payload, the way the
model-gateway grant (``llm_credentials``) already travels; the kernel signs
through the hub's private API Gateway, the gateway forwards the identity and
the hub reads the actor from the session name. The kernel cannot assume the
role itself, so it cannot present another agent's name: the backend, which
knows which agent an invocation belongs to, chooses the session name. The
actor names mirror the HMAC access keys (``agent-<id>``, ``dev-workbench``) so
hub-side logs and allowlists read the same either way. A deployment without
a caller role refuses ``iam`` attachments (``require_iam_available``) instead
of falling back to HMAC.

HMAC key pairs
--------------

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
import os
import re
import secrets
from urllib.parse import urlparse

import boto3

from app.config import settings

logger = logging.getLogger(__name__)

# The shared Actor identity for anything that is not a published agent:
# workbench sessions and Debug console runs. Its access key is the bare ID
# (no "agent-" prefix) so hub logs read unambiguously.
WORKBENCH_ACTOR_ID = "dev-workbench"

# AssumeRole session names: 2-64 chars of [A-Za-z0-9+=,.@_-]. The caller
# role's trust policy admits agent-* and dev-workbench, nothing else.
_SESSION_NAME_OK = re.compile(r"^[A-Za-z0-9+=,.@_-]{2,64}$")

# Lifetime of a caller session handed to a kernel: the AgentCore async ceiling
# (and the demo service-account token's lifespan), so a long headless run does
# not lose its tools midway — there is no refresh channel into a running agent.
# A workbench session that outlives it gets a fresh one at its next warmup. The
# caller role's MaxSessionDuration (12 h) is the ceiling; a first-hop
# web-identity exchange is what makes more than one hour possible.
IAM_SESSION_TTL_S = 8 * 3600

# the entry's invoke URL as the foundation publishes it:
# https://<api-id>-<vpce-id>.execute-api.<region>.amazonaws.com/<stage>/mcp
_ENTRY_HOST_RE = re.compile(r"^(?P<api>[a-z0-9]+)-(?P<vpce>vpce-[a-f0-9]+)\.execute-api\.(?P<region>[a-z0-9-]+)\.amazonaws\.com$")


def iam_actor(actor_id: str) -> str:
    """The IAM-path actor for an agent id (or the workbench): the AssumeRole
    session name the kernel uses, which the hub sees in x-caller-arn. Same
    naming as the HMAC access keys."""
    actor = actor_id if actor_id == WORKBENCH_ACTOR_ID else f"agent-{actor_id}"
    if not _SESSION_NAME_OK.match(actor):
        raise ValueError(f"not a valid hub actor / session name: {actor!r}")
    return actor


class McpHubCredentialsService:
    def __init__(self) -> None:
        self.sm = boto3.client("secretsmanager", region_name=settings.aws_region)
        self.sts = boto3.client("sts", region_name=settings.aws_region)
        self._account_id = ""

    @staticmethod
    def secret_name(actor_id: str) -> str:
        return f"{settings.mcp_hub_secret_prefix}/{actor_id}"

    # ------------------------------------------------------------- iam path

    @staticmethod
    def iam_actor(actor_id: str) -> str:
        return iam_actor(actor_id)

    @staticmethod
    def iam_available() -> bool:
        return bool(settings.mcp_hub_caller_role_arn)

    @staticmethod
    def require_iam_available() -> None:
        """An ``auth = iam`` attachment needs the foundation's IAM entry. Refuse
        loudly — never sign such an attachment with an HMAC pair instead."""
        if not settings.mcp_hub_caller_role_arn:
            raise ValueError(
                "mcp-hub auth=iam is unavailable: this deployment has no hub IAM entry "
                "(PLATFORM_MCP_HUB_CALLER_ROLE_ARN is empty)"
            )

    def _account(self) -> str:
        if not self._account_id:
            self._account_id = self.sts.get_caller_identity()["Account"]
        return self._account_id

    def _entry_invoke_policy(self, entry_url: str) -> str | None:
        """Session policy narrowing the grant to the one entry this attachment
        targets (the role policy is already scoped to the entry API; this makes
        the credentials themselves say so). None when the URL is not the
        foundation's entry shape — the role policy still applies."""
        try:
            parsed = urlparse(entry_url)
            m = _ENTRY_HOST_RE.match(parsed.hostname or "")
            stage = (parsed.path or "/").strip("/").split("/")[0]
            if not m or not stage:
                return None
            arn = f"arn:aws:execute-api:{m['region']}:{self._account()}:{m['api']}/{stage}/*/mcp"
        except Exception:  # noqa: BLE001 - a malformed URL just gets no session policy
            return None
        return json.dumps({
            "Version": "2012-10-17",
            "Statement": [{"Effect": "Allow", "Action": "execute-api:Invoke", "Resource": arn}],
        })

    def _assume(self, kwargs: dict) -> dict:
        """AssumeRole into the caller role without role chaining: on EKS the
        pod's IRSA web-identity token is exchanged directly (first hop, so the
        8-hour grant is possible; AssumeRoleWithWebIdentity takes no Tags).
        Outside a pod (local runs, the e2e) the plain AssumeRole path is used.
        Same shape as llm_credentials_service._assume_caller_role."""
        token_file = os.environ.get("AWS_WEB_IDENTITY_TOKEN_FILE", "")
        if token_file:
            with open(token_file, encoding="utf-8") as f:
                token = f.read().strip()
            wi = {k: v for k, v in kwargs.items() if k not in ("Tags", "TransitiveTagKeys")}
            return self.sts.assume_role_with_web_identity(WebIdentityToken=token, **wi)["Credentials"]
        return self.sts.assume_role(**kwargs)["Credentials"]

    def mint_iam_session(self, actor: str, entry_url: str = "") -> dict:
        """A caller-role session named after ``actor`` for one attachment, as
        the kernel payload carries it. The session name is what the hub sees
        as the actor, so it is chosen here — by the component that knows which
        agent (or that the workbench) is being served — and never by the
        kernel, which has no way to assume the role. Refuses (ValueError)
        rather than falling back to anything."""
        self.require_iam_available()
        if not _SESSION_NAME_OK.match(actor) or not (actor == WORKBENCH_ACTOR_ID or actor.startswith("agent-")):
            raise ValueError(f"not a hub actor the caller role admits: {actor!r}")
        kwargs: dict = {
            "RoleArn": settings.mcp_hub_caller_role_arn,
            "RoleSessionName": actor,
            "DurationSeconds": IAM_SESSION_TTL_S,
            "Tags": [{"Key": "actor", "Value": actor}],
            "TransitiveTagKeys": ["actor"],
        }
        policy = self._entry_invoke_policy(entry_url)
        if policy:
            kwargs["Policy"] = policy
        else:
            logger.warning("mcp-hub iam: %r is not the foundation's entry URL shape; minting without a session policy", entry_url)
        try:
            creds = self._assume(kwargs)
        except Exception as e:  # noqa: BLE001 - any STS failure means refuse
            logger.error("mcp-hub iam: could not mint a caller session for %s: %s", actor, e)
            raise ValueError(f"mcp-hub auth=iam: could not mint a caller session for {actor}") from e
        expiration = creds.get("Expiration")
        return {
            "access_key_id": creds["AccessKeyId"],
            "secret_access_key": creds["SecretAccessKey"],
            "session_token": creds["SessionToken"],
            "expiration": expiration.isoformat() if hasattr(expiration, "isoformat") else str(expiration or ""),
        }

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
