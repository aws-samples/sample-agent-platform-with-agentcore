"""Agent Platform control plane."""

import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header
from fastapi.middleware.cors import CORSMiddleware
from jwt import PyJWTError

from app.api import (
    agents,
    channels,
    ecosystem,
    evals,
    gateways,
    governance,
    kernels,
    memory,
    model_config,
    observability,
    pipelines,
    schedules,
    service_entry,
    sessions,
    team_demo,
)
from app.auth import verify_oidc_token
from app.config import settings
from app.context import CallerTokenMiddleware
from app.dependencies import get_current_user
from app.services.schedule_service import schedule_service

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # eventbridge mode (PortalStack): reconcile the EventBridge schedule
    # group with DynamoDB; local mode (uvicorn dev): start the tick loop.
    schedule_service.start()
    yield
    schedule_service.stop()


app = FastAPI(
    title="Agent Platform Control Plane",
    description="Sessions, kernel catalog and debug invocation for the AgentCore-backed agent platform",
    version="2.0.0",
    lifespan=lifespan,
)

# outermost: record the caller's bearer token in the request context so
# identity-forwarding attachments can use it (see app/context.py)
app.add_middleware(CallerTokenMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(sessions.router)
app.include_router(kernels.router)
app.include_router(ecosystem.router)
app.include_router(agents.router)
app.include_router(schedules.router)
app.include_router(channels.router)
app.include_router(evals.router)
app.include_router(memory.router)
app.include_router(observability.router)
app.include_router(governance.router)
app.include_router(model_config.router)
app.include_router(pipelines.router)
app.include_router(gateways.router)
app.include_router(team_demo.router)
app.include_router(service_entry.router)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/v1/me")
def whoami(
    user: str = Depends(get_current_user),
    authorization: str = Header(default=""),
):
    """The caller's identity as the backend verified it.

    In OIDC mode this includes the IdP-issued claims the platform passes on
    to identity-aware attachments (group / team membership above all), so a
    page can show *which* identity an invocation will carry.
    """
    claims: dict = {}
    if settings.oidc_issuer and authorization.startswith("Bearer "):
        try:
            claims = verify_oidc_token(authorization.removeprefix("Bearer "))
        except PyJWTError:
            claims = {}  # Cognito-authenticated internal caller
    team = claims.get("team") or claims.get("groups") or []
    return {
        "user": user,
        "is_admin": getattr(user, "is_admin", False),
        "groups": list(getattr(user, "groups", ())),
        "teams": [team] if isinstance(team, str) else [str(t).strip("/") for t in team],
        "issuer": claims.get("iss", ""),
        "audience": claims.get("aud", ""),
        "subject": claims.get("sub", ""),
    }


@app.get("/api/v1/config")
def public_config():
    """Public runtime config so the frontend needs no build-time secrets."""
    if settings.oidc_issuer:
        auth_mode = "oidc"
    elif settings.cognito_pool_id and settings.cognito_client_id:
        auth_mode = "cognito"
    else:
        auth_mode = "token" if settings.api_token else "open"
    return {
        "auth_mode": auth_mode,
        "cognito_region": settings.aws_region,
        "cognito_client_id": settings.cognito_client_id,
        "oidc_issuer": settings.oidc_issuer,
        "oidc_client_id": settings.oidc_client_id,
    }
