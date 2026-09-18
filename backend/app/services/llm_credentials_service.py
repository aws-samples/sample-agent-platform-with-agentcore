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
import json
import logging
import re
import secrets
import time
import urllib.parse

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

# ---------------------------------------------------------------- AgentCore

# STS refuses anything shorter, so this is the floor on how stale a revoked
# AgentCore session's credentials can be if the Deny below fails to apply.
AGENTCORE_TTL_S = 900

# Sessions whose AgentCore credentials have been revoked but whose STS
# expiry has not yet passed. The role's inline Deny is rebuilt from this
# partition, and entries are dropped once they expire — that pruning is what
# keeps the policy inside the 10 KB per-policy limit.
LLM_REVOKE_PK = "LLMREVOKE"

# Inline policy on the caller role carrying the per-session Deny statements.
REVOKE_POLICY_NAME = "AgentCoreSessionRevocations"

_GATEWAY_HOST_RE = re.compile(
    r"^(?P<id>[A-Za-z0-9_-]+)\.gateway\.bedrock-agentcore\.(?P<region>[a-z0-9-]+)\.amazonaws\.com$"
)


def gateway_arn_from_base_url(base_url: str, account_id: str) -> str:
    """Derive the gateway ARN from the endpoint the model config carries.

    The session policy below has to name the gateway as a resource, and asking
    an operator to keep an ARN and a URL in sync is a good way to get a
    mismatch. The URL already contains both the gateway id and the region, so
    the ARN is derivable; an unparseable host yields "" and the caller falls
    back to no session policy (the role's own policy still applies).
    """
    host = urllib.parse.urlsplit(str(base_url or "")).hostname or ""
    m = _GATEWAY_HOST_RE.match(host)
    if not m or not account_id:
        return ""
    return (
        f"arn:aws:bedrock-agentcore:{m.group('region')}:{account_id}"
        f":gateway/{m.group('id')}"
    )


def _region_from_gateway_arn(gateway_arn: str) -> str:
    """SigV4 has to be signed for the gateway's own region, which is not
    necessarily the region this backend runs in."""
    parts = str(gateway_arn or "").split(":")
    return parts[3] if len(parts) > 4 else ""


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
        self.sts = boto3.client("sts", region_name=settings.aws_region)
        self.iam = boto3.client("iam")
        self._account_id = ""

    @property
    def enabled(self) -> bool:
        return bool(settings.llm_edge_url)

    @property
    def agentcore_enabled(self) -> bool:
        return bool(settings.agentcore_gateway_caller_role_arn)

    @property
    def account_id(self) -> str:
        if not self._account_id:
            self._account_id = self.sts.get_caller_identity()["Account"]
        return self._account_id

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
        if str(spec.get("backend") or "") == "agentcore_gateway":
            return self.mint_agentcore(
                runtime_session_id, user, spec, team=team, ttl_s=ttl_s
            )
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

    # ------------------------------------------------------------ AgentCore

    def mint_agentcore(
        self,
        runtime_session_id: str,
        user: str,
        spec: dict,
        team: str = "",
        ttl_s: int = AGENTCORE_TTL_S,
        models: list[str] | None = None,
    ) -> dict | None:
        """Mint SigV4 credentials for one session's AgentCore Gateway access.

        Same shape of promise as :meth:`mint`, reached differently. There is no
        bearer token to leak here and no platform-wide secret anywhere on the
        path: the gateway holds the upstream provider credential in its token
        vault, and the container authenticates as *this session* using STS
        credentials tagged with its runtime session id.

        Two things are bound into the credential itself, so neither depends on
        the container behaving:

        - a **session policy** narrowing the grant to `InvokeGateway` on the one
          gateway this backend points at, so a leaked credential cannot reach
          any other gateway in the account;
        - a **session tag** (``session_id``), which is what
          :meth:`revoke_agentcore` conditions its Deny on. That is how one
          session is revoked without disturbing any other live session.

        What is *not* enforced here is the per-session model allowlist: IAM
        conditions cannot see a request body, so which model a session may ask
        for is not expressible as an IAM condition. The kernel does a
        fail-fast local check (an optimisation, not a boundary — the session's
        user is root in that microVM) and real enforcement needs either a
        gateway request interceptor or one gateway per model. The allowlist is
        recorded on the token item either way so the decision stays auditable.
        """
        if not self.agentcore_enabled:
            logger.error(
                "refusing to mint AgentCore gateway credentials for %s: "
                "PLATFORM_AGENTCORE_GATEWAY_CALLER_ROLE_ARN is not set",
                runtime_session_id,
            )
            return None
        base_url = str(spec.get("base_url") or "").rstrip("/")
        # ``models`` is passed only by :meth:`rotate`, which re-mints from the
        # allowlist already recorded on the token item rather than re-deriving
        # it from a spec it deliberately does not re-resolve.
        models = list(models) if models else permitted_models(spec)
        if not base_url or not models:
            logger.error(
                "refusing to mint AgentCore gateway credentials for %s: "
                "incomplete spec",
                runtime_session_id,
            )
            return None

        gateway_arn = gateway_arn_from_base_url(base_url, self.account_id)
        kwargs: dict = {
            "RoleArn": settings.agentcore_gateway_caller_role_arn,
            # The runtime session id is already caller-bound upstream (see
            # session_binding), so it is safe to use verbatim as the session
            # name — and doing so makes CloudTrail read as the session.
            "RoleSessionName": runtime_session_id[:64],
            "DurationSeconds": max(AGENTCORE_TTL_S, int(ttl_s)),
            "Tags": [{"Key": "session_id", "Value": runtime_session_id}],
            # Nothing downstream should be able to widen the grant by
            # re-assuming with a different tag.
            "TransitiveTagKeys": ["session_id"],
        }
        if gateway_arn:
            kwargs["Policy"] = json.dumps(
                {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Action": "bedrock-agentcore:InvokeGateway",
                            "Resource": gateway_arn,
                        }
                    ],
                }
            )
        else:
            logger.warning(
                "could not derive a gateway ARN from %r — minting without a "
                "session policy",
                base_url,
            )
        try:
            creds = self.sts.assume_role(**kwargs)["Credentials"]
        except Exception as e:  # noqa: BLE001 - any STS failure means refuse
            logger.error(
                "AssumeRole for AgentCore gateway session %s failed: %s",
                runtime_session_id,
                e,
            )
            return None

        expires_at = int(creds["Expiration"].timestamp())
        try:
            self.table.put_item(
                Item={
                    "PK": LLM_TOKEN_PK,
                    "SK": f"RSID#{runtime_session_id}",
                    "mode": "agentcore_gateway",
                    # No token digest: the credential is an STS session, not a
                    # bearer value this table could be used to replay.
                    "expires_at": expires_at,
                    "runtime_session_id": runtime_session_id,
                    "user": user,
                    "team": team,
                    "upstream_base_url": base_url,
                    "gateway_arn": gateway_arn,
                    "allowed_models": models,
                },
                # A grant belongs to the session's owner: create, or re-mint the
                # owner's own — never overwrite a live grant held by a different
                # principal, since doing so would stop the victim's kernel from
                # matching and is a targeted denial of service. Session ids are
                # caller-bound upstream, so a collision should be unreachable;
                # this makes it impossible even if that breaks.
                ConditionExpression="attribute_not_exists(PK) OR #u = :user",
                ExpressionAttributeNames={"#u": "user"},
                ExpressionAttributeValues={":user": user},
            )
        except self.table.meta.client.exceptions.ConditionalCheckFailedException:
            logger.error(
                "refusing to mint AgentCore gateway credentials for %s: "
                "session grant is held by a different principal",
                runtime_session_id,
            )
            return None

        return {
            "mode": "agentcore_gateway",
            "endpoint": base_url,
            "session_id": runtime_session_id,
            "region": _region_from_gateway_arn(gateway_arn) or settings.aws_region,
            # The gateway's data plane signs as bedrock-agentcore, not as the
            # control-plane service the SDK would guess from the hostname.
            "service": "bedrock-agentcore",
            "access_key_id": creds["AccessKeyId"],
            "secret_access_key": creds["SecretAccessKey"],
            "session_token": creds["SessionToken"],
            "expires_at": expires_at,
            # Carried for the kernel's fail-fast check only. The kernel must
            # not treat this as authorization — see the docstring.
            "allowed_models": models,
        }

    def revoke_agentcore(self, runtime_session_id: str) -> None:
        """Stop one session's gateway credentials from working, now.

        STS sessions cannot be withdrawn, so revocation is expressed as a Deny
        on the caller role conditioned on the session's tag. The Deny set is
        rebuilt from the LLMREVOKE partition on every call and entries whose
        STS expiry has passed are dropped, which is what keeps the inline
        policy inside its 10 KB limit: an entry only has to outlive the
        credential it revokes.
        """
        if not self.agentcore_enabled or not runtime_session_id:
            return
        item = self.table.get_item(
            Key={"PK": LLM_TOKEN_PK, "SK": f"RSID#{runtime_session_id}"}
        ).get("Item")
        expires_at = int((item or {}).get("expires_at") or 0)
        if not expires_at:
            # Nothing live to revoke; still fall through so the rebuild prunes.
            expires_at = int(time.time()) + AGENTCORE_TTL_S
        try:
            self.table.put_item(
                Item={
                    "PK": LLM_REVOKE_PK,
                    "SK": f"RSID#{runtime_session_id}",
                    "expires_at": expires_at,
                }
            )
        except Exception:  # noqa: BLE001
            logger.warning("could not record revocation for %s", runtime_session_id)
        self._rebuild_agentcore_denies()

    def _rebuild_agentcore_denies(self) -> None:
        now = int(time.time())
        live: list[str] = []
        stale: list[str] = []
        try:
            resp = self.table.query(
                KeyConditionExpression="PK = :pk AND begins_with(SK, :p)",
                ExpressionAttributeValues={":pk": LLM_REVOKE_PK, ":p": "RSID#"},
            )
        except Exception as e:  # noqa: BLE001
            logger.error("could not read revocation list: %s", e)
            return
        for it in resp.get("Items", []):
            sid = str(it.get("SK", ""))[len("RSID#") :]
            if not sid:
                continue
            # A small grace period past expiry: clock skew between here and
            # STS must not un-revoke a session a second early.
            if int(it.get("expires_at") or 0) + 60 > now:
                live.append(sid)
            else:
                stale.append(sid)

        role_name = settings.agentcore_gateway_caller_role_arn.rsplit("/", 1)[-1]
        try:
            if live:
                self.iam.put_role_policy(
                    RoleName=role_name,
                    PolicyName=REVOKE_POLICY_NAME,
                    PolicyDocument=json.dumps(
                        {
                            "Version": "2012-10-17",
                            "Statement": [
                                {
                                    "Sid": "RevokedSessions",
                                    "Effect": "Deny",
                                    "Action": "bedrock-agentcore:*",
                                    "Resource": "*",
                                    "Condition": {
                                        "StringEquals": {
                                            "aws:PrincipalTag/session_id": sorted(live)
                                        }
                                    },
                                }
                            ],
                        }
                    ),
                )
            else:
                # Nothing outstanding — drop the policy rather than leave an
                # empty statement behind.
                try:
                    self.iam.delete_role_policy(
                        RoleName=role_name, PolicyName=REVOKE_POLICY_NAME
                    )
                except self.iam.exceptions.NoSuchEntityException:
                    pass
        except Exception as e:  # noqa: BLE001
            logger.error("could not update AgentCore revocation policy: %s", e)
            return
        for sid in stale:
            try:
                self.table.delete_item(
                    Key={"PK": LLM_REVOKE_PK, "SK": f"RSID#{sid}"}
                )
            except Exception:  # noqa: BLE001
                pass

    def rotate(self, runtime_session_id: str) -> dict | None:
        """Issue a fresh token for an existing session grant.

        Deliberately does not re-resolve routing: a model-config edit applies
        on the session's next warmup, same as every other model-routing change
        on this platform. Refresh is only about keeping a live session alive.
        """
        if not runtime_session_id:
            return None
        existing = self.table.get_item(
            Key={"PK": LLM_TOKEN_PK, "SK": f"RSID#{runtime_session_id}"}
        ).get("Item")
        if existing and existing.get("mode") == "agentcore_gateway":
            # STS credentials cannot be extended, so refresh is a re-mint from
            # the routing already recorded on the item — which keeps the same
            # "config edits apply at next warmup" rule as the edge path.
            return self.mint_agentcore(
                runtime_session_id,
                str(existing.get("user") or ""),
                {
                    "backend": "agentcore_gateway",
                    "base_url": str(existing.get("upstream_base_url") or ""),
                },
                team=str(existing.get("team") or ""),
                models=[str(m) for m in (existing.get("allowed_models") or [])],
            )
        if not self.enabled:
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
        """Drop a session's grant. Called when a session ends so a credential
        scraped out of a container's memory stops working immediately rather
        than at the end of its lifetime.

        Both modes are handled. The edge mode is a single delete — the edge
        re-reads this item on every call, so the grant dies with the row. The
        AgentCore mode cannot delete an STS session, so it records a Deny
        keyed on the session's tag *before* dropping the row (the Deny needs
        the row's expiry to know when it may be pruned)."""
        if not runtime_session_id:
            return
        item = self.table.get_item(
            Key={"PK": LLM_TOKEN_PK, "SK": f"RSID#{runtime_session_id}"}
        ).get("Item")
        if item and item.get("mode") == "agentcore_gateway":
            self.revoke_agentcore(runtime_session_id)
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
