"""Shared FastAPI dependencies: identity resolution + role-based guards."""

import logging

import jwt as pyjwt
from fastapi import Depends, Header, HTTPException

from app.auth import verify_cognito_token, verify_oidc_token
from app.config import settings

logger = logging.getLogger(__name__)

# Fallback principal when no auth is configured (local development only).
DEMO_USER = "demo-user"


class Principal(str):
    """The caller's identity.

    A ``str`` subclass carrying authorization attributes, so the many
    existing ``user: str = Depends(get_current_user)`` call sites (audit
    records, quota counters, ``created_by`` stamps) keep treating it as the
    plain username while route guards read ``is_admin`` / ``groups``.
    """

    is_admin: bool = False
    is_super_admin: bool = False
    groups: tuple[str, ...] = ()


def _group_claims(claims: dict) -> list[str]:
    """Group / role memberships across IdP dialects: Cognito user-pool
    groups, a generic OIDC ``groups`` claim (Keycloak group paths keep a
    leading slash — strip it), and Keycloak realm roles."""
    raw = claims.get("cognito:groups") or claims.get("groups") or []
    if isinstance(raw, str):
        raw = [raw]
    groups = [str(g).strip("/") for g in raw if str(g).strip("/")]
    realm = claims.get("realm_access") or {}
    groups += [str(r) for r in realm.get("roles") or []]
    return groups


def _principal(
    username: str, claims: dict | None = None, *, admin: bool | None = None
) -> Principal:
    groups = _group_claims(claims or {})
    principal = Principal(username)
    principal.groups = tuple(dict.fromkeys(groups))
    if admin is None:
        # Group membership is the normal path; PLATFORM_ADMIN_USERS is the
        # escape hatch for principals that can't carry groups (the
        # schedule-runner Lambda's portal-admin delegation user).
        admin_users = {u.strip() for u in settings.admin_users.split(",") if u.strip()}
        admin = settings.admin_group in principal.groups or username in admin_users
    super_users = {u.strip() for u in settings.super_admin_users.split(",") if u.strip()}
    super_admin = (
        settings.super_admin_group in principal.groups
        or username in super_users
        # An explicit admin=True with no IdP claims is a development mode:
        # the pre-RBAC "one operator owns everything" world, so super too.
        or (claims is None and admin is True)
    )
    principal.is_super_admin = bool(super_admin)
    principal.is_admin = bool(admin or super_admin)
    return principal


def auth_mode() -> str:
    """Which of the four modes the settings select: oidc, cognito, token, open."""
    if settings.oidc_issuer:
        return "oidc"
    if settings.cognito_pool_id and settings.cognito_client_id:
        return "cognito"
    return "token" if settings.api_token else "open"


def check_auth_configuration() -> None:
    """Refuse to serve on an authentication configuration that is not what
    the operator meant. Called at startup; raises RuntimeError.

    - Cognito needs both the pool id and the client id. With one of them
      missing the request path used to skip Cognito and land in open mode,
      so a dropped environment variable made every unauthenticated caller
      the super-admin.
    - OIDC needs an audience: without one any token the realm issued for any
      client is a portal credential.
    - Open mode is a development mode and has to be enabled by
      PLATFORM_ALLOW_OPEN_AUTH=1, never reached by elimination.
    """
    if bool(settings.cognito_pool_id) != bool(settings.cognito_client_id):
        raise RuntimeError(
            "PLATFORM_COGNITO_POOL_ID and PLATFORM_COGNITO_CLIENT_ID must be set together"
        )
    if settings.oidc_issuer and not settings.oidc_audience:
        raise RuntimeError("PLATFORM_OIDC_AUDIENCE is required when PLATFORM_OIDC_ISSUER is set")
    if auth_mode() == "open" and not settings.allow_open_auth:
        raise RuntimeError(
            "no authentication configured (Cognito, OIDC or PLATFORM_API_TOKEN); "
            "set PLATFORM_ALLOW_OPEN_AUTH=1 to run the open development mode on purpose"
        )


def get_current_user(authorization: str = Header(default="")) -> Principal:
    """Resolve the caller's identity (username + roles).

    Auth modes, in priority order:
      1. Generic OIDC (``PLATFORM_OIDC_ISSUER`` set) — verify the Bearer
         access token against the issuer's JWKS, identity =
         ``preferred_username`` (falls back to ``sub``).
      2. Cognito (``PLATFORM_COGNITO_POOL_ID`` set) — verify the Bearer ID
         token, identity = the Cognito username.
      3. Static token (``PLATFORM_API_TOKEN`` set) — exact Bearer match.
      4. Open — local development only.

    Admin role: membership of ``settings.admin_group`` in the token's group
    claims, or the username appearing in ``settings.admin_users``. Super-admin
    (``is_super_admin``): the same test against ``super_admin_group`` /
    ``super_admin_users`` — the tier that may manage other users' schedules.
    Modes 3/4
    have no IdP and are development modes — the caller is treated as admin so
    a local backend behaves like the pre-RBAC platform.
    """
    if settings.oidc_issuer:
        if not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Missing Bearer token")
        token = authorization.removeprefix("Bearer ")
        try:
            claims = verify_oidc_token(token)
            return _principal(claims.get("preferred_username") or claims["sub"], claims)
        except pyjwt.PyJWTError as oidc_err:
            # Chain to Cognito when it is also configured: platform-internal
            # callers (the schedule-runner Lambda's portal-admin delegation)
            # still authenticate against the Cognito pool in OIDC mode.
            if settings.cognito_pool_id and settings.cognito_client_id:
                try:
                    claims = verify_cognito_token(token)
                    return _principal(
                        claims.get("cognito:username") or claims["sub"], claims
                    )
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
        return _principal(claims.get("cognito:username") or claims["sub"], claims)

    if settings.api_token:
        if authorization != f"Bearer {settings.api_token}":
            raise HTTPException(status_code=401, detail="Invalid or missing token")
        return _principal(DEMO_USER, admin=True)

    # Open mode. Startup already refused this unless PLATFORM_ALLOW_OPEN_AUTH
    # is set; the check is repeated per request so a process that somehow got
    # here with a half-configured IdP serves 503, not a super-admin.
    if not settings.allow_open_auth or auth_mode() != "open":
        raise HTTPException(status_code=503, detail="authentication is not configured")
    return _principal(DEMO_USER, admin=True)


def require_admin(user: Principal = Depends(get_current_user)) -> Principal:
    """Route guard for the platform's management surface (channels,
    scheduler, governance, registry writes, …). Returns the principal so
    handlers can keep using it as the username."""
    if not getattr(user, "is_admin", False):
        raise HTTPException(status_code=403, detail="Administrator role required")
    return user
