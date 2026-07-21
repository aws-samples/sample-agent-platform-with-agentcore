"""Shared FastAPI dependencies."""

import logging

import jwt as pyjwt
from fastapi import Header, HTTPException

from app.auth import verify_cognito_token
from app.config import settings

logger = logging.getLogger(__name__)

# Fallback principal when no auth is configured (local development only).
DEMO_USER = "demo-user"


def get_current_user(authorization: str = Header(default="")) -> str:
    """Resolve the caller's identity.

    Auth modes, in priority order:
      1. Cognito (``PLATFORM_COGNITO_POOL_ID`` set) — verify the Bearer ID
         token, identity = the Cognito ``sub`` claim.
      2. Static token (``PLATFORM_API_TOKEN`` set) — exact Bearer match.
      3. Open — local development only.
    """
    if settings.cognito_pool_id and settings.cognito_client_id:
        if not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Missing Bearer token")
        try:
            claims = verify_cognito_token(authorization.removeprefix("Bearer "))
        except pyjwt.PyJWTError as e:
            raise HTTPException(status_code=401, detail=f"Invalid token: {e}")
        # Prefer the username: readable in audit logs / quotas, and stable
        # within a pool. Falls back to the opaque sub claim.
        return claims.get("cognito:username") or claims["sub"]

    if settings.api_token:
        if authorization != f"Bearer {settings.api_token}":
            raise HTTPException(status_code=401, detail="Invalid or missing token")
        return DEMO_USER

    return DEMO_USER
