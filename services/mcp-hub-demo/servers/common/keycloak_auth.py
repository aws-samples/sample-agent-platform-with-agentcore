"""Keycloak JWT verification for backend MCP servers.

Each backend verifies the caller's token INDEPENDENTLY — it does not trust
the hub. Signature (via the IdP's JWKS), issuer, audience and expiry are all
checked. The verified claims ride on the SDK's AccessToken so tool functions
can make their own data-authorization decisions via `departments()`.
"""

import anyio
import jwt
from jwt import PyJWKClient

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken, TokenVerifier


class KeycloakTokenVerifier(TokenVerifier):
    def __init__(self, *, jwks_url: str, issuer: str, audience: str):
        self.issuer = issuer
        self.audience = audience
        self.jwk_client = PyJWKClient(jwks_url, cache_keys=True)

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            claims = await anyio.to_thread.run_sync(self._decode, token)
        except jwt.PyJWTError:
            return None
        return AccessToken(
            token=token,
            client_id=str(claims.get("azp", "unknown")),
            scopes=str(claims.get("scope", "")).split(),
            expires_at=claims.get("exp"),
            subject=claims.get("sub"),
            claims=claims,
        )

    def _decode(self, token: str) -> dict:
        signing_key = self.jwk_client.get_signing_key_from_jwt(token)
        return jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            issuer=self.issuer,
            audience=self.audience,
            options={"require": ["exp", "iat", "iss", "aud"]},
        )


def caller_claims() -> dict:
    token = get_access_token()
    return dict(token.claims or {}) if token else {}


def departments() -> set[str]:
    """Departments of the calling user, from the verified `department` claim."""
    value = caller_claims().get("department") or []
    if isinstance(value, str):
        value = [value]
    return set(value)


def username() -> str:
    return caller_claims().get("preferred_username", "unknown")
