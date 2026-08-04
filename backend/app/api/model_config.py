"""Model backend control plane: config + connectivity test."""

import logging
import time

from fastapi import APIRouter, Depends, HTTPException

from app.dependencies import get_current_user, require_admin
from app.models.schemas import ModelConfigUpdate, ModelTestRequest
from app.services import invocation_service
from app.services.audit_service import audit_service
from app.services.governance_service import QuotaExceeded, SourceDisabled
from app.services.model_config_service import model_config_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/model-config", tags=["model-config"])


@router.get("")
def get_config(user: str = Depends(get_current_user)):
    return model_config_service.get_config()


@router.put("")
def update_config(req: ModelConfigUpdate, user: str = Depends(require_admin)):
    try:
        cfg = model_config_service.update_config(req.model_dump(exclude_none=True))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    audit_service.record(
        user, "model.config.update", "model-config",
        str(req.model_dump(exclude_none=True))[:400],
    )
    return cfg


@router.post("/test")
def test_backend(req: ModelTestRequest, user: str = Depends(require_admin)):
    """Connectivity check: one minimal governed invocation routed through the
    chosen backend. The reply proves the whole chain (backend config → kernel
    env → model) round-trips; the run also lands in the invocation ledger."""
    try:
        spec = model_config_service.resolve(req.backend, req.model)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    started = time.monotonic()
    try:
        res = invocation_service.invoke(
            user=user,
            source="debug",
            target="agent-sdk",
            prompt=(
                "Connectivity check. Reply with exactly one line: "
                "ok, I am <the model name/id you are running as>"
            ),
            max_turns=1,
            ref="model-config-test",
            # bedrock default (spec None) still forces an explicit routing
            # test — never silently fall back to "whatever the container has"
            model_spec=spec or {"backend": "bedrock"},
        )
    except (QuotaExceeded, SourceDisabled) as e:
        raise HTTPException(status_code=429, detail=str(e))
    except Exception:
        # Details go to the log, never into the HTTP response body.
        logger.exception("model connectivity test failed (%s:%s)", req.backend, req.model)
        return {
            "ok": False, "backend": req.backend, "model": req.model,
            "reply": "", "duration_ms": int((time.monotonic() - started) * 1000),
            "error": "connectivity test failed — see backend logs",
        }
    usage = res.get("usage") or {}
    return {
        "ok": bool(res.get("ok")),
        "backend": req.backend,
        "model": (spec or {}).get("model", "") or req.model,
        "reply": (res.get("result") or "")[:500],
        "duration_ms": usage.get("duration_ms")
        or int((time.monotonic() - started) * 1000),
        "cost_usd": usage.get("total_cost_usd"),
        "error": "" if res.get("ok") else str(res.get("raw", {}))[:300],
    }
