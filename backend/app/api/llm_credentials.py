"""Kernel-facing renewal of AgentCore gateway credentials.

Every AgentCore gateway credential lives 15 minutes, and a session is revoked
by no longer renewing it (see ``llm_credentials_service.renew_agentcore``).
This is the route a headless kernel renews through; the workbench kernel uses
``POST /api/v1/sessions/workspace-credentials``, which renews both its grants
in one call.

Authenticated by the renewal token delivered with the credential, not by a
user identity: the caller is a kernel, which has none. Deliberately NOT behind
get_current_user, like the workspace-credentials route. Every refusal is the
same 401 so the response does not distinguish "revoked", "bad token" and
"grant expired". A 429 means only "this session renewed less than a minute
ago, retry": several invocations can share one session's grant, so one of
them renewing must not end the others' grant.
"""

from fastapi import APIRouter, HTTPException

from app.services.llm_credentials_service import RenewThrottled, llm_credentials_service

router = APIRouter(prefix="/api/v1/llm-credentials", tags=["llm-credentials"])


@router.post("/renew")
def renew(body: dict):
    try:
        # passed as sent: the service refuses anything not shaped like a
        # session id and a token, rather than this coercing a list or a
        # number into a string that might look like one
        creds = llm_credentials_service.renew_agentcore(
            body.get("runtime_session_id", ""), body.get("token", "")
        )
    except RenewThrottled as e:
        raise HTTPException(
            status_code=429, detail="Renewed too recently; retry",
            headers={"Retry-After": str(max(1, e.retry_after))},
        )
    if not creds:
        raise HTTPException(status_code=401, detail="Renewal refused")
    return {"llm_credentials": creds}
