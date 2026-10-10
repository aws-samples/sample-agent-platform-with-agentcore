"""HR MCP server (MCP-2 in the architecture diagram).

The hub only routes hr-department callers here, but this server does NOT
blindly trust the hub: it re-verifies the caller's JWT and re-checks the
`department` claim itself (defense in depth). If the hub is ever
misconfigured or bypassed, HR data still stays locked.
"""

import os

from pydantic import AnyHttpUrl

from mcp.server import MCPServer
from mcp.server.auth.settings import AuthSettings

from common.keycloak_auth import KeycloakTokenVerifier, departments, username

ISSUER = os.environ.get("ISSUER", "http://localhost:8080/realms/mcp-demo")
JWKS_URL = os.environ.get("JWKS_URL", f"{ISSUER}/protocol/openid-connect/certs")
AUDIENCE = os.environ.get("AUDIENCE", "http://localhost:8000/mcp")
PORT = int(os.environ.get("PORT", "9002"))

mcp = MCPServer(
    "hr-mcp",
    token_verifier=KeycloakTokenVerifier(
        jwks_url=JWKS_URL, issuer=ISSUER, audience=AUDIENCE
    ),
    auth=AuthSettings(
        issuer_url=AnyHttpUrl(ISSUER),
        resource_server_url=AnyHttpUrl(AUDIENCE),
    ),
)

EMPLOYEES = [
    {"name": "Alice Zhang", "department": "sales", "level": "L5", "location": "Singapore"},
    {"name": "Bob Chen", "department": "finance", "level": "L6", "location": "Hong Kong"},
    {"name": "Carol Wu", "department": "hr", "level": "L5", "location": "Shanghai"},
    {"name": "David Lin", "department": "sales", "level": "L4", "location": "Taipei"},
    {"name": "Eve Huang", "department": "engineering", "level": "L6", "location": "Beijing"},
]


def _require_hr():
    if "hr" not in departments():
        raise PermissionError(
            "HR data is restricted to the hr department. "
            "This backend re-checks the caller's JWT claim itself and does not "
            "blindly trust the hub's routing."
        )


@mcp.tool()
def search_employee(name: str) -> dict:
    """Look up employees whose name contains the given text (HR only)."""
    _require_hr()
    matches = [e for e in EMPLOYEES if name.lower() in e["name"].lower()]
    return {"caller": username(), "matches": matches}


@mcp.tool()
def list_headcount() -> dict:
    """Headcount per department (HR only)."""
    _require_hr()
    counts: dict[str, int] = {}
    for e in EMPLOYEES:
        counts[e["department"]] = counts.get(e["department"], 0) + 1
    return {"caller": username(), "headcount": counts}


if __name__ == "__main__":
    mcp.run(
        transport="streamable-http",
        host="0.0.0.0",
        port=PORT,
        stateless_http=True,
        json_response=True,
    )
