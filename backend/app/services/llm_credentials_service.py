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
  credential-expiry cadence, so this adds no new endpoint and no second secret;
- for a headless kernel on the AgentCore path, through
  ``POST /api/v1/llm-credentials/renew`` with the renewal token delivered in
  the grant (a headless kernel has no workspace refresh token).

An AgentCore credential lives 15 minutes and is revoked by refusing to renew
it, so every renewal path checks that the grant row still exists.

A token that leaks buys nothing off-platform: the edge listener is internal to
the VPC with no public route, the grant expires within the hour, and it is
scoped to one session's model allowance.
"""

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
import urllib.parse

import boto3

from app.config import settings
from app.services import retention, session_binding
from app.services.dynamo import query_all

logger = logging.getLogger(__name__)

# Matches the workspace credential lifetime so one refresh call renews both.
TOKEN_TTL_S = 3600

# One item per live runtime session. Keyed for get_item, like WSTOKEN: this
# table is shared (sessions, channels, ledger, audit), and a filtered scan
# reads a single 1 MB page of *unfiltered* data, so past that size the matching
# session silently stops being found.
LLM_TOKEN_PK = "LLMTOKEN"

# ---------------------------------------------------------------- AgentCore

# Lifetime of every AgentCore gateway credential: the shortest STS issues.
# This *is* the revocation bound. An STS session cannot be withdrawn, so a
# session is revoked by no longer renewing it (see renew_agentcore), and a
# credential scraped out of a container stays usable for at most this long
# after its session ends. The previous design kept the credential long-lived
# and revoked through a per-session Deny list on the caller role; that list
# grew with the revocation rate and IAM caps a role's inline policies at
# 10,240 characters in total, so past roughly 85 live entries revocation
# stopped applying (Continuum C008, with C041 / C042 in the same mechanism).
AGENTCORE_TTL_S = 900

# At most one STS call per session in this interval, however many kernels or
# requests share the renewal token, so a holder of the token cannot turn it
# into an STS call per request. A renewal inside it is throttled, not refused:
# several invocations share one grant row, and one of them renewing must not
# end another's grant. Kernels start renewing 5 minutes before expiry, which
# leaves room for several retries past this.
AGENTCORE_RENEW_MIN_INTERVAL_S = 60


class RenewThrottled(Exception):
    """A renewal arrived within AGENTCORE_RENEW_MIN_INTERVAL_S of the last one.

    Not a refusal: the grant is alive and the caller should retry after
    ``retry_after`` seconds.
    """

    def __init__(self, retry_after: int):
        super().__init__(f"retry after {retry_after}s")
        self.retry_after = retry_after

# Inline policy the previous design kept on the caller role. Nothing writes it
# any more; the name stays so the cutover runbook (docs/deployment.md) can
# refer to it.
LEGACY_REVOKE_POLICY_NAME = "AgentCoreSessionRevocations"

# AgentCore runtimeSessionId: 33-100 characters of [A-Za-z0-9_-]; and the
# renewal token's own shape (hex SHA-256).
_RSID_RE = re.compile(r"[A-Za-z0-9_-]{1,100}")
_RENEW_TOKEN_RE = re.compile(r"[0-9a-f]{64}")

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


def _renew_token(runtime_session_id: str, nonce: str) -> str:
    """The kernel's renewal credential for one AgentCore grant.

    Derived rather than stored: an HMAC under the platform binding secret over
    the session id and the row's nonce. Every invocation on one warm session
    gets the same token (the nonce survives re-mints), and deleting the row
    invalidates all of them at once. A reader of the table learns nothing
    replayable without the binding secret.
    """
    return hmac.new(
        session_binding._secret(),
        b"llm-renew\x00" + runtime_session_id.encode() + b"\x00" + nonce.encode(),
        hashlib.sha256,
    ).hexdigest()


def _agentcore_block(item: dict, creds: dict, expires_at: int) -> dict:
    """The kernel's credential block for an AgentCore grant row."""
    runtime_session_id = str(item.get("runtime_session_id") or "")
    gateway_arn = str(item.get("gateway_arn") or "")
    return {
        "mode": "agentcore_gateway",
        "endpoint": str(item.get("upstream_base_url") or ""),
        "session_id": runtime_session_id,
        "region": _region_from_gateway_arn(gateway_arn) or settings.aws_region,
        # The gateway's data plane signs as bedrock-agentcore, not as the
        # control-plane service the SDK would guess from the hostname.
        "service": "bedrock-agentcore",
        "access_key_id": creds["AccessKeyId"],
        "secret_access_key": creds["SecretAccessKey"],
        "session_token": creds["SessionToken"],
        "expires_at": expires_at,
        # Carried for the kernel's fail-fast check only. The kernel must not
        # treat this as authorization — see mint_agentcore's docstring.
        "allowed_models": [str(m) for m in (item.get("allowed_models") or [])],
        # How the kernel keeps the grant alive past one credential. Empty url =
        # this deployment has no route back from the runtime, and the kernel
        # simply runs until the credential expires.
        "renew": {
            "url": settings.llm_renew_url,
            "token": _renew_token(runtime_session_id, str(item.get("renew_nonce") or "")),
            "grant_expires_at": int(item.get("grant_expires_at") or 0),
        },
    }


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
        try:
            self.table.put_item(
                Item={
                    "PK": LLM_TOKEN_PK,
                    "SK": f"RSID#{runtime_session_id}",
                    # Only the digest is stored: a reader of this table cannot
                    # replay the grant.
                    "token_sha256": _sha256(token),
                    "expires_at": expires_at,
                    retention.TTL_ATTR: retention.ttl_after_expiry(expires_at),
                    "runtime_session_id": runtime_session_id,
                    "user": user,
                    "team": team,
                    "upstream_base_url": base_url,
                    "gateway_secret_name": secret_name,
                    "allowed_models": models,
                },
                # A grant belongs to the session's owner. Only mint a new one
                # or re-mint the owner's own — never overwrite a live grant held
                # by a different principal. Session ids are already caller-bound
                # upstream (see session_binding), so a collision here should be
                # unreachable; this makes overwriting another user's grant (and
                # the targeted DoS it enables) impossible even if that breaks.
                ConditionExpression="attribute_not_exists(PK) OR #u = :user",
                ExpressionAttributeNames={"#u": "user"},
                ExpressionAttributeValues={":user": user},
            )
        except self.table.meta.client.exceptions.ConditionalCheckFailedException:
            logger.error(
                "refusing to mint gateway credentials for %s: session grant is "
                "held by a different principal",
                runtime_session_id,
            )
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

    # ------------------------------------------------------------ AgentCore

    def mint_agentcore(
        self,
        runtime_session_id: str,
        user: str,
        spec: dict,
        team: str = "",
        ttl_s: int = AGENTCORE_TTL_S,
        models: list[str] | None = None,
        renew_of: dict | None = None,
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
        - the **session identity**: the role session name is the runtime
          session id (and, off EKS, a ``session_id`` session tag carries it
          too), so CloudTrail attributes every gateway call to its session.

        Every credential lives :data:`AGENTCORE_TTL_S` (15 minutes) whatever
        ``ttl_s`` says. ``ttl_s`` is instead the *grant* lifetime: how long the
        kernel may keep renewing through :meth:`renew_agentcore`, bounded so
        a renewal token that leaks out of a headless run (which nothing ever
        revokes explicitly) stops working when the run's ceiling passes.
        Revoking a session deletes its row, which stops renewal; the
        credential already in the container then dies within 15 minutes.

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
            "DurationSeconds": AGENTCORE_TTL_S,
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
            creds = self._assume_caller_role(kwargs)
        except Exception as e:  # noqa: BLE001 - any STS failure means refuse
            logger.error(
                "AssumeRole for AgentCore gateway session %s failed: %s",
                runtime_session_id,
                e,
            )
            return None

        expires_at = int(creds["Expiration"].timestamp())
        now = int(time.time())
        if renew_of is not None:
            return self._record_renewal(runtime_session_id, renew_of, creds, expires_at)
        # The grant row outlives every credential issued under it: a renewal
        # is refused past ``grant_expires_at``, and the last credential can
        # have been issued just before that.
        grant_expires_at = now + max(AGENTCORE_TTL_S, int(ttl_s))
        try:
            item = self.table.update_item(
                Key={"PK": LLM_TOKEN_PK, "SK": f"RSID#{runtime_session_id}"},
                # SET, not put_item: two invocations on one warm session mint
                # concurrently, and the nonce the renewal token is derived from
                # must survive the second one, or the first invocation's
                # renewals would start failing. if_not_exists keeps it; a
                # revoke deletes the row and with it every token derived from
                # the old nonce.
                UpdateExpression=(
                    "SET #m = :mode, expires_at = :e, grant_expires_at = :g, "
                    "#ttl = :ttl, runtime_session_id = :sid, #u = :user, "
                    "team = :team, upstream_base_url = :base, "
                    "gateway_arn = :garn, allowed_models = :models, "
                    "renew_nonce = if_not_exists(renew_nonce, :nonce)"
                ),
                # A grant belongs to the session's owner: create, or re-mint the
                # owner's own — never overwrite a live grant held by a different
                # principal, since doing so would stop the victim's kernel from
                # matching and is a targeted denial of service. Session ids are
                # caller-bound upstream, so a collision should be unreachable;
                # this makes it impossible even if that breaks.
                ConditionExpression="attribute_not_exists(PK) OR #u = :user",
                ExpressionAttributeNames={"#m": "mode", "#ttl": retention.TTL_ATTR, "#u": "user"},
                ExpressionAttributeValues={
                    ":mode": "agentcore_gateway",
                    ":e": expires_at,
                    ":g": grant_expires_at,
                    ":ttl": retention.ttl_after_expiry(grant_expires_at + AGENTCORE_TTL_S),
                    ":sid": runtime_session_id,
                    ":user": user,
                    ":team": team,
                    ":base": base_url,
                    ":garn": gateway_arn,
                    ":models": models,
                    ":nonce": secrets.token_urlsafe(16),
                },
                ReturnValues="ALL_NEW",
            )["Attributes"]
        except self.table.meta.client.exceptions.ConditionalCheckFailedException:
            logger.error(
                "refusing to mint AgentCore gateway credentials for %s: "
                "session grant is held by a different principal",
                runtime_session_id,
            )
            return None

        return _agentcore_block(item, creds, expires_at)

    def _claim_renew_slot(self, runtime_session_id: str, row: dict, now: int) -> None:
        """Take this session's renewal slot or raise RenewThrottled.

        Conditional on the row still carrying the nonce the caller was
        authorized against, so a slot cannot be claimed on a row a revoke and
        reconnect replaced.
        """
        last = int(row.get("last_renewed_at") or 0)
        if now - last < AGENTCORE_RENEW_MIN_INTERVAL_S:
            raise RenewThrottled(AGENTCORE_RENEW_MIN_INTERVAL_S - (now - last))
        try:
            self.table.update_item(
                Key={"PK": LLM_TOKEN_PK, "SK": f"RSID#{runtime_session_id}"},
                UpdateExpression="SET last_renewed_at = :now",
                ConditionExpression=(
                    "attribute_exists(PK) AND renew_nonce = :n AND "
                    "(attribute_not_exists(last_renewed_at) OR last_renewed_at <= :cut)"
                ),
                ExpressionAttributeValues={
                    ":now": now,
                    ":n": str(row.get("renew_nonce") or ""),
                    ":cut": now - AGENTCORE_RENEW_MIN_INTERVAL_S,
                },
            )
        except self.table.meta.client.exceptions.ConditionalCheckFailedException:
            # Another renewal took the slot (or the row was replaced; the
            # conditional write after STS then discards this one).
            raise RenewThrottled(AGENTCORE_RENEW_MIN_INTERVAL_S)

    def _record_renewal(self, runtime_session_id: str, row: dict, creds: dict, expires_at: int) -> dict | None:
        """Record a renewed credential on the grant row, if the grant survives.

        The row was read before STS was called. A revoke can delete it in
        between, and an unconditional write would then *create* a fresh grant
        and undo the revocation. So the write requires the row to exist with
        the nonce the renewal was authorized against, and it changes only the
        credential's expiry: never the ceiling, the routing or the owner.
        """
        try:
            item = self.table.update_item(
                Key={"PK": LLM_TOKEN_PK, "SK": f"RSID#{runtime_session_id}"},
                UpdateExpression="SET expires_at = :e",
                ConditionExpression="attribute_exists(PK) AND renew_nonce = :n AND #u = :user",
                ExpressionAttributeNames={"#u": "user"},
                ExpressionAttributeValues={
                    ":e": expires_at,
                    ":n": str(row.get("renew_nonce") or ""),
                    ":user": str(row.get("user") or ""),
                },
                ReturnValues="ALL_NEW",
            )["Attributes"]
        except self.table.meta.client.exceptions.ConditionalCheckFailedException:
            logger.info("renewal of %s lost to a revoke; credential discarded", runtime_session_id)
            return None
        return _agentcore_block(item, creds, expires_at)

    def _assume_caller_role(self, kwargs: dict) -> dict:
        """AssumeRole into the caller role without role chaining.

        On EKS the backend's own identity comes from an IRSA web-identity
        token, so the same token is exchanged directly for the caller role: a
        first hop rather than a chained session. AssumeRoleWithWebIdentity
        takes no Tags (session tags come from the token's claims), so the
        session is identified by its RoleSessionName, the runtime session id.
        Outside a pod (local runs, the e2e) there is no token and plain
        AssumeRole with the ``session_id`` tag is used.
        """
        token_file = os.environ.get("AWS_WEB_IDENTITY_TOKEN_FILE", "")
        if token_file:
            with open(token_file) as f:
                token = f.read().strip()
            wi = {k: v for k, v in kwargs.items() if k not in ("Tags", "TransitiveTagKeys")}
            return self.sts.assume_role_with_web_identity(
                WebIdentityToken=token, **wi
            )["Credentials"]
        return self.sts.assume_role(**kwargs)["Credentials"]

    def renew_agentcore(self, runtime_session_id: str, token: str) -> dict | None:
        """Kernel-facing renewal: a fresh 15-minute credential for a live grant.

        This is where revocation happens. ``revoke`` deletes the row, so a
        renewal after it finds nothing and the session's last credential runs
        out within :data:`AGENTCORE_TTL_S`. Refused (None) when:

        - the row is gone (revoked, or never an AgentCore grant);
        - the token was not derived from this row's nonce (constant-time);
        - the grant's own lifetime has passed.

        Raises :class:`RenewThrottled` when the session renewed less than
        :data:`AGENTCORE_RENEW_MIN_INTERVAL_S` ago. The slot is claimed with a
        conditional write *before* STS is called, so concurrent renewals of
        one session cost one STS call between them.

        The re-mint routes from what the row recorded, never from the request,
        and keeps the grant's original lifetime: renewal extends a credential,
        never the grant.
        """
        if not self.agentcore_enabled:
            return None
        # Unauthenticated input: anything that is not shaped like a runtime
        # session id and a renewal token is refused before it reaches
        # DynamoDB, where an oversized key is a ValidationException (a 500,
        # not the uniform refusal).
        if not isinstance(runtime_session_id, str) or not _RSID_RE.fullmatch(runtime_session_id):
            return None
        if not isinstance(token, str) or not _RENEW_TOKEN_RE.fullmatch(token):
            return None
        item = self.table.get_item(
            Key={"PK": LLM_TOKEN_PK, "SK": f"RSID#{runtime_session_id}"},
            ConsistentRead=True,
        ).get("Item")
        if not item or item.get("mode") != "agentcore_gateway":
            return None
        expected = _renew_token(runtime_session_id, str(item.get("renew_nonce") or ""))
        if not item.get("renew_nonce") or not hmac.compare_digest(expected, str(token)):
            return None
        now = int(time.time())
        grant_expires_at = int(item.get("grant_expires_at") or 0)
        if grant_expires_at <= now:
            return None
        self._claim_renew_slot(runtime_session_id, item, now)
        return self.mint_agentcore(
            runtime_session_id,
            str(item.get("user") or ""),
            {
                "backend": "agentcore_gateway",
                "base_url": str(item.get("upstream_base_url") or ""),
            },
            team=str(item.get("team") or ""),
            models=[str(m) for m in (item.get("allowed_models") or [])],
            renew_of=item,
        )

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
            # The workbench's refresh call (authenticated by the workspace
            # refresh token, which stop/delete drops). Same rule as
            # renew_agentcore: a fresh credential inside the grant's own
            # lifetime, routed from the row, never a longer grant.
            now = int(time.time())
            grant_expires_at = int(existing.get("grant_expires_at") or 0)
            if grant_expires_at <= now:
                return None
            try:
                self._claim_renew_slot(runtime_session_id, existing, now)
            except RenewThrottled:
                # The workbench kernel renews its model credential on its own
                # clock through /llm-credentials/renew; this combined refresh
                # just skips the model half when that renewed a moment ago.
                return None
            return self.mint_agentcore(
                runtime_session_id,
                str(existing.get("user") or ""),
                {
                    "backend": "agentcore_gateway",
                    "base_url": str(existing.get("upstream_base_url") or ""),
                },
                team=str(existing.get("team") or ""),
                models=[str(m) for m in (existing.get("allowed_models") or [])],
                renew_of=existing,
            )
        if not self.enabled:
            return None
        token = secrets.token_urlsafe(32)
        expires_at = int(time.time()) + TOKEN_TTL_S
        try:
            self.table.update_item(
                Key={"PK": LLM_TOKEN_PK, "SK": f"RSID#{runtime_session_id}"},
                # Rotation extends validity, so it must extend the row's TTL too.
                UpdateExpression="SET token_sha256 = :h, expires_at = :e, #ttl = :ttl",
                ConditionExpression="attribute_exists(PK)",
                ExpressionAttributeNames={"#ttl": retention.TTL_ATTR},
                ExpressionAttributeValues={
                    ":h": _sha256(token),
                    ":e": expires_at,
                    ":ttl": retention.ttl_after_expiry(expires_at),
                },
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
        scraped out of a container's memory stops working as soon as possible.

        One delete for both modes. The edge re-reads this row on every call, so
        an edge grant dies with it. An AgentCore STS session cannot be
        withdrawn: deleting the row stops every renewal (renew_agentcore and
        rotate both need it), so the credential the container holds runs out
        within :data:`AGENTCORE_TTL_S`. That 15-minute bound is the design,
        not a gap — see the module constants for why it replaced a Deny list.
        """
        if not runtime_session_id:
            return
        self.table.delete_item(
            Key={"PK": LLM_TOKEN_PK, "SK": f"RSID#{runtime_session_id}"}
        )

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
