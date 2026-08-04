"""Channel endpoints.

Management endpoints require the platform-admin role; the webhook
endpoint authenticates with the channel's own token so external systems can
call it without AWS or Cognito credentials.
"""

from fastapi import APIRouter, Depends, Header, HTTPException

from app.dependencies import get_current_user, require_admin
from app.models.schemas import (
    ChannelCallersUpdateRequest,
    ChannelCreateRequest,
    ChannelWebhookRequest,
)
from app.services.audit_service import audit_service
from app.services.channel_service import channel_service
from app.services.governance_service import QuotaExceeded, SourceDisabled

router = APIRouter(prefix="/api/v1/channels", tags=["channels"])


@router.get("")
def list_channels(user: str = Depends(require_admin)):
    return channel_service.list_channels()


@router.post("")
def create_channel(req: ChannelCreateRequest, user: str = Depends(require_admin)):
    try:
        channel = channel_service.create_channel(
            user=user, name=req.name, target=req.target, description=req.description,
            kind=req.kind, allowed_caller_arns=req.allowed_caller_arns,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    audit_service.record(
        user, "channel.create", f"channel:{channel['name']}", f"{req.kind}:{req.target}"
    )
    return channel  # token channels include the token — the only time it is returned


@router.put("/{channel_id}/callers")
def update_channel_callers(
    channel_id: str, req: ChannelCallersUpdateRequest, user: str = Depends(require_admin)
):
    """Rebind an iam channel's caller allowlist — the channel-level
    authorization lives here, so granting/revoking a workload's access to an
    agent is a platform operation, not an IAM change."""
    try:
        channel = channel_service.set_allowed_callers(channel_id, req.allowed_caller_arns)
    except PermissionError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
    audit_service.record(
        user, "channel.callers", f"channel:{channel['name']}",
        f"{len(channel['allowed_caller_arns'])} role(s)",
    )
    return channel


@router.post("/{channel_id}/enable")
def enable_channel(channel_id: str, user: str = Depends(require_admin)):
    channel = channel_service.set_enabled(channel_id, True)
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
    audit_service.record(user, "channel.enable", f"channel:{channel['name']}")
    return channel


@router.post("/{channel_id}/disable")
def disable_channel(channel_id: str, user: str = Depends(require_admin)):
    channel = channel_service.set_enabled(channel_id, False)
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
    audit_service.record(user, "channel.disable", f"channel:{channel['name']}")
    return channel


@router.delete("/{channel_id}")
def delete_channel(channel_id: str, user: str = Depends(require_admin)):
    if not channel_service.delete_channel(channel_id):
        raise HTTPException(status_code=404, detail="Channel not found")
    audit_service.record(user, "channel.delete", f"channel:{channel_id}")
    return {"ok": True}


@router.get("/{channel_id}/sop")
def channel_sop(channel_id: str, user: str = Depends(require_admin)):
    """The ops runbook for onboarding a workload to the service entry: a
    one-time API-wide IAM grant + EKS Pod Identity steps + SigV4 submit/poll
    sample code. Channel-level authorization is the channel's caller
    allowlist, managed here in the platform."""
    from app.services.sop_service import render_sop

    channel = channel_service.get_channel(channel_id)
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
    if channel.get("kind") != "iam":
        raise HTTPException(status_code=400, detail="SOPs apply to IAM channels only")
    return {"channel_id": channel_id, "markdown": render_sop(channel)}


@router.post("/{channel_id}/test")
def test_channel(
    channel_id: str,
    req: ChannelWebhookRequest,
    user: str = Depends(require_admin),
):
    """In-portal webhook test: same routing as the webhook, authenticated by
    the portal identity instead of the channel token (which is only shown
    once at creation)."""
    try:
        result = channel_service.test_channel(
            channel_id, user=user, message=req.message, conversation_id=req.conversation_id
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Channel not found")
    except (QuotaExceeded, SourceDisabled) as e:
        raise HTTPException(status_code=429, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=403, detail=str(e))
    return {
        "ok": result.get("ok", False),
        "reply": result.get("result", ""),
        "runtime_session_id": result.get("runtime_session_id", ""),
    }


@router.post("/{channel_id}/webhook")
def webhook(
    channel_id: str,
    req: ChannelWebhookRequest,
    x_channel_token: str = Header(default=""),
):
    """External entry point — authenticated by the channel token, not Cognito."""
    try:
        result = channel_service.handle_webhook(
            channel_id, x_channel_token, req.message, req.conversation_id
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Channel not found")
    except PermissionError:
        raise HTTPException(status_code=401, detail="Invalid channel token")
    except (QuotaExceeded, SourceDisabled) as e:
        raise HTTPException(status_code=429, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=403, detail=str(e))
    return {
        "ok": result.get("ok", False),
        "reply": result.get("result", ""),
        "runtime_session_id": result.get("runtime_session_id", ""),
    }
