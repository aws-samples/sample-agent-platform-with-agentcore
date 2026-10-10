"""Order MCP server (MCP-1 in the architecture diagram).

Demonstrates DATA-LEVEL authorization living in the backend, not the hub:
the hub only decided that sales and finance may reach this server at all.
Which orders (and which fields) a caller sees is decided HERE, from the
`department` claim in the caller's own verified JWT.

  - finance sees every order, including commercial amounts
  - sales sees only sales-owned orders, with amounts redacted
"""

import os

from pydantic import AnyHttpUrl

from mcp.server import MCPServer
from mcp.server.auth.settings import AuthSettings

from common.keycloak_auth import KeycloakTokenVerifier, departments, username

ISSUER = os.environ.get("ISSUER", "http://localhost:8080/realms/mcp-demo")
JWKS_URL = os.environ.get("JWKS_URL", f"{ISSUER}/protocol/openid-connect/certs")
AUDIENCE = os.environ.get("AUDIENCE", "http://localhost:8000/mcp")
PORT = int(os.environ.get("PORT", "9001"))

mcp = MCPServer(
    "order-mcp",
    token_verifier=KeycloakTokenVerifier(
        jwks_url=JWKS_URL, issuer=ISSUER, audience=AUDIENCE
    ),
    auth=AuthSettings(
        issuer_url=AnyHttpUrl(ISSUER),
        resource_server_url=AnyHttpUrl(AUDIENCE),
    ),
)

ORDERS = [
    {"order_id": "ORD-1001", "customer": "Acme Corp", "region": "APAC",
     "owner_department": "sales", "status": "shipped", "amount_usd": 125000},
    {"order_id": "ORD-1002", "customer": "Globex", "region": "EMEA",
     "owner_department": "sales", "status": "processing", "amount_usd": 87000},
    {"order_id": "ORD-1003", "customer": "Initech", "region": "APAC",
     "owner_department": "sales", "status": "delivered", "amount_usd": 43000},
    {"order_id": "ORD-2001", "customer": "Umbrella Ltd", "region": "AMER",
     "owner_department": "finance", "status": "invoiced", "amount_usd": 310000},
    {"order_id": "ORD-2002", "customer": "Stark Industries", "region": "AMER",
     "owner_department": "finance", "status": "paid", "amount_usd": 990000},
]


def _visible_orders() -> list[dict]:
    depts = departments()
    if "finance" in depts:
        # finance sees everything, including amounts
        return [dict(o) for o in ORDERS]
    if "sales" in depts:
        # sales sees only sales-owned orders, amounts redacted
        return [
            {k: v for k, v in o.items() if k != "amount_usd"}
            for o in ORDERS
            if o["owner_department"] == "sales"
        ]
    return []


@mcp.tool()
def list_orders() -> dict:
    """List the orders the calling user is allowed to see."""
    return {
        "caller": username(),
        "departments": sorted(departments()),
        "orders": _visible_orders(),
    }


@mcp.tool()
def get_order(order_id: str) -> dict:
    """Fetch a single order by id, subject to the same data authorization."""
    for order in _visible_orders():
        if order["order_id"] == order_id:
            return order
    return {"error": f"order {order_id} not found or not visible to your department"}


if __name__ == "__main__":
    mcp.run(
        transport="streamable-http",
        host="0.0.0.0",
        port=PORT,
        stateless_http=True,
        json_response=True,
    )
