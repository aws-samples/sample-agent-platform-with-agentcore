"""Channel endpoints.

Management endpoints require the portal identity (Cognito); the webhook
endpoint authenticates with the channel's own token so external systems can
call it without AWS or Cognito credentials.
"""

from fastapi import APIRouter, Depends, Header, HTTPException

from app.dependencies import get_current_user
from app.models.schemas import ChannelCreateRequest, ChannelWebhookRequest
from app.services.audit_service import audit_service
from app.services.channel_service import channel_service
from app.services.governance_service import QuotaExceeded, SourceDisabled

router = APIRouter(prefix="/api/v1/channels", tags=["channels"])


@router.get("")
def list_channels(user: str = Depends(get_current_user)):
    return channel_service.list_channels()


@router.post("")
def create_channel(req: ChannelCreateRequest, user: str = Depends(get_current_user)):
    channel = channel_service.create_channel(
        user=user, name=req.name, target=req.target, description=req.description
    )
    audit_service.record(user, "channel.create", f"channel:{channel['name']}", req.target)
    return channel  # includes the token — the only time it is returned


@router.post("/{channel_id}/enable")
def enable_channel(channel_id: str, user: str = Depends(get_current_user)):
    channel = channel_service.set_enabled(channel_id, True)
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
    audit_service.record(user, "channel.enable", f"channel:{channel['name']}")
    return channel


@router.post("/{channel_id}/disable")
def disable_channel(channel_id: str, user: str = Depends(get_current_user)):
    channel = channel_service.set_enabled(channel_id, False)
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
    audit_service.record(user, "channel.disable", f"channel:{channel['name']}")
    return channel


@router.delete("/{channel_id}")
def delete_channel(channel_id: str, user: str = Depends(get_current_user)):
    if not channel_service.delete_channel(channel_id):
        raise HTTPException(status_code=404, detail="Channel not found")
    audit_service.record(user, "channel.delete", f"channel:{channel_id}")
    return {"ok": True}


@router.post("/{channel_id}/test")
def test_channel(
    channel_id: str,
    req: ChannelWebhookRequest,
    user: str = Depends(get_current_user),
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
