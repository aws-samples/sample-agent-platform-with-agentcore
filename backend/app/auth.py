"""Bearer-token verification (Cognito or a generic OIDC provider).

Two JWKS-based verifiers share the same shape:

- **Cognito** (the default portal mode): the frontend signs in with
  USER_PASSWORD_AUTH and sends the resulting **ID token**. Verification
  checks the signature against the pool's JWKS (cached), the issuer, the
  audience (app client ID) and the ``token_use`` claim.
- **Generic OIDC** (``PLATFORM_OIDC_ISSUER`` set — e.g. the Keycloak realm
  from TeamAuthStack): the frontend runs an authorization-code + PKCE flow
  and sends the **access token**. Verification checks the signature against
  the issuer's JWKS (resolved via OIDC discovery), the issuer, and — when
  ``PLATFORM_OIDC_AUDIENCE`` is set — the audience. The access token (not
  the ID token) is used so the *same* credential the portal accepts can be
  forwarded to JWT-protected AgentCore runtimes and gateways, keeping the
  IdP-issued claims (e.g. ``team``) intact end to end.
"""

import json
import logging
import urllib.request
from functools import lru_cache

import jwt
from jwt import PyJWKClient

from app.config import settings

logger = logging.getLogger(__name__)


# ------------------------------- Cognito --------------------------------


@lru_cache(maxsize=1)
def _cognito_jwk_client() -> PyJWKClient:
    issuer = (
        f"https://cognito-idp.{settings.aws_region}.amazonaws.com/"
        f"{settings.cognito_pool_id}"
    )
    return PyJWKClient(f"{issuer}/.well-known/jwks.json", cache_keys=True)


def verify_cognito_token(token: str) -> dict:
    """Return the verified claims, or raise ``jwt.PyJWTError``."""
    issuer = (
        f"https://cognito-idp.{settings.aws_region}.amazonaws.com/"
        f"{settings.cognito_pool_id}"
    )
    signing_key = _cognito_jwk_client().get_signing_key_from_jwt(token)
    claims = jwt.decode(
        token,
        signing_key.key,
        algorithms=["RS256"],
        audience=settings.cognito_client_id,
        issuer=issuer,
    )
    if claims.get("token_use") != "id":
        raise jwt.InvalidTokenError("expected an ID token")
    return claims


# ----------------------------- generic OIDC ------------------------------


@lru_cache(maxsize=1)
def _oidc_jwk_client() -> PyJWKClient:
    discovery = f"{settings.oidc_issuer.rstrip('/')}/.well-known/openid-configuration"
    with urllib.request.urlopen(discovery) as resp:  # nosec B310 - https issuer from config
        jwks_uri = json.load(resp)["jwks_uri"]
    return PyJWKClient(jwks_uri, cache_keys=True)


def verify_oidc_token(token: str) -> dict:
    """Verify an access token issued by the configured OIDC provider."""
    issuer = settings.oidc_issuer.rstrip("/")
    signing_key = _oidc_jwk_client().get_signing_key_from_jwt(token)
    kwargs: dict = {"algorithms": ["RS256"], "issuer": issuer}
    if settings.oidc_audience:
        kwargs["audience"] = settings.oidc_audience
    else:
        kwargs["options"] = {"verify_aud": False}
    return jwt.decode(token, signing_key.key, **kwargs)
