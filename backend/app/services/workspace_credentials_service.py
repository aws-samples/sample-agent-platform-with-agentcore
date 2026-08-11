"""Per-session S3 credentials for the interactive kernel's workspace sync.

The interactive kernel's own execution role deliberately has **no** access to
``workspaces/*`` (docs/permissions.md §2): anything running inside a session's
microVM can read the container role's credentials from the metadata endpoint,
and AgentCore execution roles are runtime-scoped, so an IAM condition can't
tell one session from another. Instead, the backend — which owns the
session↔user mapping — assumes ``agent-platform-workspace-access`` with an
inline **session policy** narrowing S3 to ``workspaces/{runtimeSessionId}/*``
and hands the resulting credentials to that session's container:

- at warmup, inside the ``/invocations`` payload (``workspace_credentials``);
- afterwards through ``POST /api/v1/sessions/workspace-credentials``,
  authenticated by a per-session refresh token minted here (constant-time
  compared, same pattern as channel tokens) — the container has no Cognito
  identity, and role chaining caps each grant at 1 hour, so syncs outliving
  the first hour need this path.

The session policy can only *narrow* the assumed role's permissions, so even
a bug here can never grant more than ``workspaces/*``.
"""

import hmac
import logging
import secrets

import boto3

from app.config import settings

logger = logging.getLogger(__name__)

# Role chaining (task role → workspace-access role) caps sessions at 1h.
CREDS_DURATION_S = 3600

# Partition for the runtime_session_id → refresh-token lookup items.
WS_TOKEN_PK = "WSTOKEN"


def _session_policy(runtime_session_id: str) -> str:
    import json

    prefix = f"{settings.workspace_prefix}/{runtime_session_id}"
    bucket_arn = f"arn:aws:s3:::{settings.workspace_bucket}"
    return json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "SessionObjects",
                    "Effect": "Allow",
                    "Action": [
                        "s3:GetObject",
                        "s3:PutObject",
                        "s3:AbortMultipartUpload",
                    ],
                    "Resource": f"{bucket_arn}/{prefix}/*",
                },
                {
                    "Sid": "SessionList",
                    "Effect": "Allow",
                    "Action": "s3:ListBucket",
                    "Resource": bucket_arn,
                    "Condition": {
                        "StringLike": {"s3:prefix": [f"{prefix}/*", f"{prefix}/"]}
                    },
                },
            ],
        }
    )


class WorkspaceCredentialsService:
    def __init__(self) -> None:
        self.sts = boto3.client("sts", region_name=settings.aws_region)
        dynamodb = boto3.resource("dynamodb", region_name=settings.aws_region)
        self.table = dynamodb.Table(settings.dynamo_table)

    @property
    def enabled(self) -> bool:
        return bool(settings.workspace_access_role_arn and settings.workspace_bucket)

    def mint(self, runtime_session_id: str) -> dict | None:
        """AssumeRole with the session-scoped policy. Returns the credential
        block for the kernel, or None when the role isn't configured (local
        dev against a stack that predates it)."""
        if not self.enabled:
            return None
        resp = self.sts.assume_role(
            RoleArn=settings.workspace_access_role_arn,
            # Recognizable in CloudTrail; [\w+=,.@-]{2,64}
            RoleSessionName=f"ws-{runtime_session_id[:59]}",
            Policy=_session_policy(runtime_session_id),
            DurationSeconds=CREDS_DURATION_S,
        )
        c = resp["Credentials"]
        return {
            "access_key_id": c["AccessKeyId"],
            "secret_access_key": c["SecretAccessKey"],
            "session_token": c["SessionToken"],
            "expiration": c["Expiration"].isoformat(),
        }

    # ------------------------------------------------ refresh-token flow

    def issue_refresh_token(self, user: str, session_id: str) -> str:
        """Mint (or rotate) the per-session refresh token.

        Written twice: onto the session item (under the user's partition, as
        before) and into a ``WSTOKEN`` lookup item keyed by the
        ``runtime_session_id`` the container presents on refresh. The lookup
        item is what lets :meth:`refresh` be a ``get_item`` instead of a
        table scan — a filtered scan reads a single 1 MB page of *unfiltered*
        data, and this table is shared (sessions, channels, ledger, audit),
        so past 1 MB the matching session silently stops being found and
        workspace sync dies after the first hour with no error anywhere.
        """
        token = secrets.token_urlsafe(32)
        attrs = self.table.update_item(
            Key={"PK": f"USER#{user}", "SK": f"SESSION#{session_id}"},
            UpdateExpression="SET ws_refresh_token = :t",
            ExpressionAttributeValues={":t": token},
            ReturnValues="ALL_NEW",
        ).get("Attributes", {})
        runtime_session_id = str(attrs.get("runtime_session_id", ""))
        if runtime_session_id:
            self.table.put_item(
                Item={
                    "PK": WS_TOKEN_PK,
                    "SK": f"RSID#{runtime_session_id}",
                    "ws_refresh_token": token,
                    "runtime_session_id": runtime_session_id,
                    "user": user,
                    "session_id": session_id,
                }
            )
        return token

    def refresh(self, runtime_session_id: str, token: str) -> dict | None:
        """Container-facing refresh: find the session by runtime_session_id,
        constant-time-compare the token, mint fresh credentials.

        Returns None on any mismatch — the caller maps that to 401 without
        distinguishing "unknown session" from "bad token".
        """
        if not runtime_session_id or not token:
            return None
        expected = self._lookup_token(runtime_session_id)
        if not expected or not hmac.compare_digest(expected, token):
            return None
        return self.mint(runtime_session_id)

    def _lookup_token(self, runtime_session_id: str) -> str:
        """The stored refresh token for a runtime session, or "".

        Direct key read against the WSTOKEN partition. Sessions whose token
        was issued before the lookup item existed fall back to a fully
        paginated scan, so a workspace already in flight keeps refreshing
        across the deploy of this change; the fallback goes cold once those
        sessions' credentials expire (1 h) and connect() reissues.
        """
        item = self.table.get_item(
            Key={"PK": WS_TOKEN_PK, "SK": f"RSID#{runtime_session_id}"}
        ).get("Item")
        if item:
            return str(item.get("ws_refresh_token", ""))
        return self._legacy_scan_token(runtime_session_id)

    def _legacy_scan_token(self, runtime_session_id: str) -> str:
        """Pre-migration fallback: walk every page. The original single-page
        scan silently missed sessions once the table passed 1 MB, and treated
        "several matches" like "none" — both still fail closed here."""
        kwargs: dict = {
            "FilterExpression": "runtime_session_id = :r",
            "ExpressionAttributeValues": {":r": runtime_session_id},
        }
        found: list[dict] = []
        while True:
            resp = self.table.scan(**kwargs)
            found.extend(i for i in resp.get("Items", []) if i.get("ws_refresh_token"))
            if len(found) > 1:
                logger.error(
                    "ambiguous runtime_session_id %s — refusing to mint credentials",
                    runtime_session_id,
                )
                return ""
            start_key = resp.get("LastEvaluatedKey")
            if not start_key:
                break
            kwargs["ExclusiveStartKey"] = start_key
        return str(found[0].get("ws_refresh_token", "")) if len(found) == 1 else ""


workspace_credentials_service = WorkspaceCredentialsService()
