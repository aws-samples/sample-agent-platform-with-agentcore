"""IAM service entry: server-to-server invocation via the API Gateway front door.

Trust chain, in order:

1. **API Gateway (AWS_IAM)** authenticates the caller with SigV4 — an EKS
   Pod Identity role, a Lambda role, any IAM principal — and relays two
   headers to the backend: ``x-caller-arn`` ($context.identity.userArn,
   gateway-verified) and ``x-service-entry-secret`` (a Secrets Manager value
   only the gateway integration knows). No token exists to leak.
2. **This router** admits a request only with the correct entry secret, so a
   direct internet hit on the same path (the backend sits behind CloudFront)
   cannot forge a caller ARN.
3. **channel_service.authorize_service_caller** checks the channel is an
   ``iam`` channel, enabled, and — when an allowlist is configured — that the
   caller's role is on it.
4. Optional **robot identity** (``x-robot-token``): the POD's own IdP access
   token (client-credentials service account). SigV4 answers "is this our
   infrastructure"; this token answers "which business identity is calling" —
   it is verified against the platform's OIDC issuer and then forwarded to
   identity-aware MCP attachments ({{user_token}}), exactly like a signed-in
   portal user's token. It rides a custom header because SigV4 owns
   ``Authorization``.

The contract is submit/poll (a synchronous agent run outlives front-door
timeouts): POST returns 202 + invocation_id, GET returns the record.
"""

import hmac
import logging

import boto3
import jwt as pyjwt
from fastapi import APIRouter, Header, HTTPException

from app.auth import verify_oidc_token
from app.config import settings
from app.models.schemas import ChannelWebhookRequest
from app.services.channel_service import channel_service, normalize_caller_arn
from app.services.service_invocation_service import service_invocation_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/service/v1", tags=["service-entry"])

_secret_cache: str | None = None


def _entry_secret() -> str:
    """The gateway-injected shared secret (env override for local dev,
    Secrets Manager otherwise; cached per process, retried on failure)."""
    global _secret_cache
    if settings.service_entry_secret:
        return settings.service_entry_secret
    if _secret_cache is None:
        try:
            sm = boto3.client("secretsmanager", region_name=settings.aws_region)
            _secret_cache = sm.get_secret_value(
                SecretId=settings.service_entry_secret_name
            )["SecretString"]
        except Exception:
            logger.exception(
                "could not read %s — service entry disabled",
                settings.service_entry_secret_name,
            )
            return ""
    return _secret_cache


def _authenticate(secret_header: str, caller_header: str) -> str:
    expected = _entry_secret()
    if not expected or not hmac.compare_digest(expected, secret_header or ""):
        raise HTTPException(
            status_code=401,
            detail="service entry calls must come through the API Gateway front door",
        )
    caller = normalize_caller_arn(caller_header or "")
    if not caller.startswith("arn:"):
        raise HTTPException(status_code=401, detail="missing verified caller identity")
    return caller


def _verify_robot_token(token: str) -> str:
    """Robot identity is opt-in per request; when present it must verify
    against the platform's OIDC issuer (fail fast — never forward an
    unverified credential downstream)."""
    if not token:
        return ""
    if not settings.oidc_issuer:
        raise HTTPException(
            status_code=400,
            detail="robot identity requires the platform to run in OIDC mode",
        )
    try:
        verify_oidc_token(token)
    except pyjwt.PyJWTError as e:
        raise HTTPException(status_code=401, detail=f"invalid robot token: {e}")
    return token


@router.post("/channels/{channel_id}/invocations", status_code=202)
def submit_invocation(
    channel_id: str,
    req: ChannelWebhookRequest,
    x_caller_arn: str = Header(default=""),
    x_service_entry_secret: str = Header(default=""),
    x_robot_token: str = Header(default=""),
):
    caller = _authenticate(x_service_entry_secret, x_caller_arn)
    robot_token = _verify_robot_token(x_robot_token)
    try:
        item = channel_service.authorize_service_caller(channel_id, caller)
    except KeyError:
        raise HTTPException(status_code=404, detail="Channel not found")
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    record = service_invocation_service.submit(
        channel_item=item,
        channel_id=channel_id,
        caller=caller,
        message=req.message,
        conversation_id=req.conversation_id,
        robot_token=robot_token,
    )
    return {
        "invocation_id": record["invocation_id"],
        "status": record["status"],
        "poll": f"/service/v1/invocations/{record['invocation_id']}",
    }


@router.get("/invocations/{invocation_id}")
def get_invocation(
    invocation_id: str,
    x_caller_arn: str = Header(default=""),
    x_service_entry_secret: str = Header(default=""),
):
    caller = _authenticate(x_service_entry_secret, x_caller_arn)
    try:
        return service_invocation_service.get(invocation_id, caller=caller)
    except KeyError:
        raise HTTPException(status_code=404, detail="Invocation not found")
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
