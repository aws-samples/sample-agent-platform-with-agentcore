"""Agent Platform control plane."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import (
    agents,
    channels,
    ecosystem,
    evals,
    governance,
    kernels,
    memory,
    observability,
    pipelines,
    schedules,
    sessions,
)
from app.config import settings
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
app.include_router(pipelines.router)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/v1/config")
def public_config():
    """Public runtime config so the frontend needs no build-time secrets."""
    auth_mode = (
        "cognito"
        if settings.cognito_pool_id and settings.cognito_client_id
        else ("token" if settings.api_token else "open")
    )
    return {
        "auth_mode": auth_mode,
        "cognito_region": settings.aws_region,
        "cognito_client_id": settings.cognito_client_id,
    }
