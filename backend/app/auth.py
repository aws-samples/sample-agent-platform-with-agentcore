"""Cognito JWT verification.

The frontend signs in with USER_PASSWORD_AUTH and sends the resulting
**ID token** as ``Authorization: Bearer <token>``. Verification checks the
signature against the pool's JWKS (cached), the issuer, the audience
(app client ID) and the ``token_use`` claim.
"""

import logging
from functools import lru_cache

import jwt
from jwt import PyJWKClient

from app.config import settings

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _jwk_client() -> PyJWKClient:
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
    signing_key = _jwk_client().get_signing_key_from_jwt(token)
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
