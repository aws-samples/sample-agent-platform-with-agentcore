"""Per-session model-gateway credentials for kernels.

Same reasoning as :mod:`app.services.workspace_credentials_service`, applied to
model access. Anything inside a session's microVM is reachable by whoever holds
that session's terminal: process environment, files, process memory, and the
execution role's credentials from the metadata endpoint. So the kernel role has
no read on the gateway secret, and the gateway key never reaches a container.

Instead the kernel talks to ``llm-edge``, an internal service that holds the
key, and authenticates with a short-lived token minted here. What that token
permits is written onto the token item by this service, not sent by the
container: the upstream base URL, the secret to inject, and the exact set of
model names the session may request. The edge re-reads that item on every call,
so nothing a container says about its own routing is trusted.

Delivery mirrors the workspace-credential flow:

- at warmup, inside the ``/invocations`` payload (``llm_credentials``);
- afterwards through ``POST /api/v1/sessions/workspace-credentials``, which
  rotates both grants in one call — the container already polls it on the
  credential-expiry cadence, so this adds no new endpoint and no second secret.

A token that leaks buys nothing off-platform: the edge listener is internal to
the VPC with no public route, the grant expires within the hour, and it is
scoped to one session's model allowance.
"""

import hashlib
import hmac
import logging
import secrets
import time

import boto3

from app.config import settings

logger = logging.getLogger(__name__)

# Matches the workspace credential lifetime so one refresh call renews both.
TOKEN_TTL_S = 3600

# One item per live runtime session. Keyed for get_item, like WSTOKEN: this
# table is shared (sessions, channels, ledger, audit), and a filtered scan
# reads a single 1 MB page of *unfiltered* data, so past that size the matching
# session silently stops being found.
LLM_TOKEN_PK = "LLMTOKEN"


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def permitted_models(spec: dict) -> list[str]:
    """The model names a gateway-mode session is allowed to request.

    Exactly what the kernel is configured to be able to send: the session's
    resolved model, its small/fast companion, and the ``/model`` picker's
    opus/sonnet/haiku aliases (which ``model_config_service.resolve`` already
    picked from this backend's catalog). Not the whole catalog — a session was
    routed to a model, and widening that here would undo the routing.
    """
    names = {str(spec.get("model") or "")}
    names.add(str(spec.get("small_fast_model") or ""))
    aliases = spec.get("alias_models")
    if isinstance(aliases, dict):
        names.update(str(v) for v in aliases.values())
    return sorted(n for n in names if n)


class LlmCredentialsService:
    def __init__(self) -> None:
        dynamodb = boto3.resource("dynamodb", region_name=settings.aws_region)
        self.table = dynamodb.Table(settings.dynamo_table)

    @property
    def enabled(self) -> bool:
        return bool(settings.llm_edge_url)

    def mint(
        self,
        runtime_session_id: str,
        user: str,
        spec: dict,
        team: str = "",
        ttl_s: int = TOKEN_TTL_S,
    ) -> dict | None:
        """Record a session's gateway entitlements and return the kernel's
        credential block, or None when the edge isn't deployed.

        Callers must treat None as "gateway routing is unavailable" and refuse
        the session. Falling back to the old behaviour would mean handing the
        container the key this service exists to keep out of it.

        ``ttl_s`` exists for headless async runs, which have no refresh channel
        and may legitimately execute for hours; the grant has to outlive the
        run or the agent loses model access midway. That is a smaller
        concession than it looks, because in the headless kernel the grant
        never reaches the agent's own subprocess environment either way — the
        kernel keeps it and hands the CLI a container-local token instead.
        """
        if not self.enabled:
            return None
        base_url = str(spec.get("base_url") or "")
        secret_name = str(spec.get("secret_name") or "")
        models = permitted_models(spec)
        if not base_url or not secret_name or not models:
            logger.error(
                "refusing to mint gateway credentials for %s: incomplete spec",
                runtime_session_id,
            )
            return None

        token = secrets.token_urlsafe(32)
        expires_at = int(time.time()) + max(60, int(ttl_s))
        self.table.put_item(
            Item={
                "PK": LLM_TOKEN_PK,
                "SK": f"RSID#{runtime_session_id}",
                # Only the digest is stored: a reader of this table cannot
                # replay the grant.
                "token_sha256": _sha256(token),
                "expires_at": expires_at,
                "runtime_session_id": runtime_session_id,
                "user": user,
                "team": team,
                "upstream_base_url": base_url,
                "gateway_secret_name": secret_name,
                "allowed_models": models,
            }
        )
        return {
            "endpoint": settings.llm_edge_url,
            # Echoed back by the kernel as the x-platform-session-id header so
            # the edge can find this grant. Carried in the block rather than
            # left for the kernel to figure out, because the headless kernel
            # does not otherwise know the session ID the backend chose.
            "session_id": runtime_session_id,
            "token": token,
            "expires_at": expires_at,
        }

    def rotate(self, runtime_session_id: str) -> dict | None:
        """Issue a fresh token for an existing session grant.

        Deliberately does not re-resolve routing: a model-config edit applies
        on the session's next warmup, same as every other model-routing change
        on this platform. Refresh is only about keeping a live session alive.
        """
        if not self.enabled or not runtime_session_id:
            return None
        token = secrets.token_urlsafe(32)
        expires_at = int(time.time()) + TOKEN_TTL_S
        try:
            self.table.update_item(
                Key={"PK": LLM_TOKEN_PK, "SK": f"RSID#{runtime_session_id}"},
                UpdateExpression="SET token_sha256 = :h, expires_at = :e",
                ConditionExpression="attribute_exists(PK)",
                ExpressionAttributeValues={":h": _sha256(token), ":e": expires_at},
            )
        except Exception:
            # No grant for this session (never gateway-routed, or revoked).
            # Not an error: Bedrock-direct sessions take this path too.
            return None
        return {
            "endpoint": settings.llm_edge_url,
            # Echoed back by the kernel as the x-platform-session-id header so
            # the edge can find this grant. Carried in the block rather than
            # left for the kernel to figure out, because the headless kernel
            # does not otherwise know the session ID the backend chose.
            "session_id": runtime_session_id,
            "token": token,
            "expires_at": expires_at,
        }

    def revoke(self, runtime_session_id: str) -> None:
        """Drop a session's grant. Called when a session ends so a token
        scraped out of a container's memory stops working immediately rather
        than at the end of its hour."""
        if not runtime_session_id:
            return
        try:
            self.table.delete_item(
                Key={"PK": LLM_TOKEN_PK, "SK": f"RSID#{runtime_session_id}"}
            )
        except Exception:
            logger.warning("could not revoke gateway grant for %s", runtime_session_id)

    def verify(self, runtime_session_id: str, token: str) -> dict | None:
        """Backend-side counterpart of the edge's check. Not used on the model
        data path (the edge does its own lookup); kept for the platform's own
        diagnostics and tests."""
        if not runtime_session_id or not token:
            return None
        item = self.table.get_item(
            Key={"PK": LLM_TOKEN_PK, "SK": f"RSID#{runtime_session_id}"}
        ).get("Item")
        if not item:
            return None
        stored = str(item.get("token_sha256", ""))
        if not stored or not hmac.compare_digest(stored, _sha256(token)):
            return None
        if int(item.get("expires_at", 0)) < int(time.time()):
            return None
        return item


llm_credentials_service = LlmCredentialsService()
