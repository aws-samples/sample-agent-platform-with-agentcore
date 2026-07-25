"""Shared FastAPI dependencies."""

import logging

import jwt as pyjwt
from fastapi import Header, HTTPException

from app.auth import verify_cognito_token, verify_oidc_token
from app.config import settings

logger = logging.getLogger(__name__)

# Fallback principal when no auth is configured (local development only).
DEMO_USER = "demo-user"


def get_current_user(authorization: str = Header(default="")) -> str:
    """Resolve the caller's identity.

    Auth modes, in priority order:
      1. Generic OIDC (``PLATFORM_OIDC_ISSUER`` set) — verify the Bearer
         access token against the issuer's JWKS, identity =
         ``preferred_username`` (falls back to ``sub``).
      2. Cognito (``PLATFORM_COGNITO_POOL_ID`` set) — verify the Bearer ID
         token, identity = the Cognito ``sub`` claim.
      3. Static token (``PLATFORM_API_TOKEN`` set) — exact Bearer match.
      4. Open — local development only.
    """
    if settings.oidc_issuer:
        if not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Missing Bearer token")
        token = authorization.removeprefix("Bearer ")
        try:
            claims = verify_oidc_token(token)
            return claims.get("preferred_username") or claims["sub"]
        except pyjwt.PyJWTError as oidc_err:
            # Chain to Cognito when it is also configured: platform-internal
            # callers (the schedule-runner Lambda's portal-admin delegation)
            # still authenticate against the Cognito pool in OIDC mode.
            if settings.cognito_pool_id and settings.cognito_client_id:
                try:
                    claims = verify_cognito_token(token)
                    return claims.get("cognito:username") or claims["sub"]
                except pyjwt.PyJWTError:
                    pass
            raise HTTPException(status_code=401, detail=f"Invalid token: {oidc_err}")

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
