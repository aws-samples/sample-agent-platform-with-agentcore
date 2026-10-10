"""End-to-end test of the SSO-authenticated MCP hub.

Runs against a full stack (docker compose up, or the three local processes +
Keycloak). Mints user tokens through the password grant on the `e2e-tester`
client — that client exists only so this script can skip the browser; the
interactive flow Claude Code uses is the CIMD/PKCE flow.

Usage:  python scripts/e2e.py
Env:    KEYCLOAK_URL (default http://localhost:8080)
        HUB_URL      (default http://localhost:8000/mcp)
        ORDER_URL    (default http://localhost:9001/mcp)
"""

import asyncio
import contextlib
import json
import os
import sys

import httpx2

from mcp import Client
from mcp.client.streamable_http import streamable_http_client

KEYCLOAK_URL = os.environ.get("KEYCLOAK_URL", "http://localhost:8080")
HUB_URL = os.environ.get("HUB_URL", "http://localhost:8000/mcp")
ORDER_URL = os.environ.get("ORDER_URL", "http://localhost:9001/mcp")
TOKEN_URL = f"{KEYCLOAK_URL}/realms/mcp-demo/protocol/openid-connect/token"

USERS = {
    "alice": "alice-demo-password",  # sales
    "bob": "bob-demo-password",      # finance
    "carol": "carol-demo-password",  # hr
}

PASSED, FAILED = [], []


def check(name: str, condition: bool, detail: str = ""):
    (PASSED if condition else FAILED).append(name)
    print(f"  {'PASS' if condition else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not condition else ""))


def payload(result) -> dict:
    """Tool return value: structured_content if present, else the JSON text."""
    if result.structured_content:
        return result.structured_content
    for block in result.content:
        if getattr(block, "text", None):
            return json.loads(block.text)
    return {}


def get_token(username: str) -> str:
    resp = httpx2.post(
        TOKEN_URL,
        data={
            "grant_type": "password",
            "client_id": "e2e-tester",
            "username": username,
            "password": USERS[username],
        },
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


@contextlib.asynccontextmanager
async def hub_client(token: str):
    async with httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {token}"},
        timeout=httpx2.Timeout(30.0, read=300.0),
        follow_redirects=True,
    ) as http_client:
        async with Client(streamable_http_client(HUB_URL, http_client=http_client)) as client:
            yield client


async def main() -> int:
    print("== discovery & unauthenticated access ==")
    prm_url = HUB_URL.replace("/mcp", "/.well-known/oauth-protected-resource/mcp")
    prm = httpx2.get(prm_url).json()
    check("PRM advertises the IdP", prm.get("authorization_servers") == [f"{KEYCLOAK_URL}/realms/mcp-demo"], str(prm))
    r = httpx2.post(HUB_URL, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    check("no token -> 401", r.status_code == 401, str(r.status_code))
    check("401 carries resource_metadata pointer", "resource_metadata=" in r.headers.get("www-authenticate", ""), r.headers.get("www-authenticate", "<none>"))
    r = httpx2.post(HUB_URL, json={}, headers={"Authorization": "Bearer garbage"})
    check("tampered token -> 401", r.status_code == 401, str(r.status_code))
    r = httpx2.post(ORDER_URL, json={})
    check("backend directly, no token -> 401", r.status_code == 401, str(r.status_code))

    tokens = {u: get_token(u) for u in USERS}

    print("== alice (sales): order tools only, amounts redacted ==")
    async with hub_client(tokens["alice"]) as client:
        names = sorted(t.name for t in (await client.list_tools()).tools)
        check("alice sees only order tools", names == ["order__get_order", "order__list_orders"], str(names))
        result = await client.call_tool("order__list_orders", {})
        data = payload(result)
        orders = data.get("orders", [])
        check("alice sees only sales-owned orders", bool(orders) and all(o["owner_department"] == "sales" for o in orders), str(data)[:200])
        check("alice sees no amounts", all("amount_usd" not in o for o in orders))
        check("backend reports caller identity", data.get("caller") == "alice", str(data.get("caller")))
        denied = await client.call_tool("hr__search_employee", {"name": "Carol"})
        check("alice calling hr tool is denied by hub", denied.is_error and "may not access" in str(denied.content), str(denied.content)[:200])

    print("== bob (finance): all orders with amounts ==")
    async with hub_client(tokens["bob"]) as client:
        names = sorted(t.name for t in (await client.list_tools()).tools)
        check("bob sees only order tools", names == ["order__get_order", "order__list_orders"], str(names))
        result = await client.call_tool("order__list_orders", {})
        orders = payload(result).get("orders", [])
        check("bob sees all 5 orders", len(orders) == 5, str(len(orders)))
        check("bob sees amounts", all("amount_usd" in o for o in orders))

    print("== carol (hr): hr tools only ==")
    async with hub_client(tokens["carol"]) as client:
        names = sorted(t.name for t in (await client.list_tools()).tools)
        check("carol sees only hr tools", names == ["hr__list_headcount", "hr__search_employee"], str(names))
        result = await client.call_tool("hr__list_headcount", {})
        counts = payload(result).get("headcount", {})
        check("carol gets headcount", counts.get("sales") == 2, str(counts))
        denied = await client.call_tool("order__list_orders", {})
        check("carol calling order tool is denied by hub", denied.is_error and "may not access" in str(denied.content), str(denied.content)[:200])

    print("== defense in depth: backend re-checks the claim itself ==")
    # Call the HR BACKEND directly (bypassing the hub) with a non-hr token.
    async with httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {tokens['alice']}"},
        timeout=httpx2.Timeout(30.0, read=300.0),
        follow_redirects=True,
    ) as http_client:
        hr_direct = os.environ.get("HR_URL", "http://localhost:9002/mcp")
        async with Client(streamable_http_client(hr_direct, http_client=http_client)) as client:
            result = await client.call_tool("search_employee", {"name": "Carol"})
            check(
                "hr backend refuses alice even without the hub",
                result.is_error and "restricted to the hr department" in str(result.content),
                str(result.content)[:200],
            )

    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        print("failed:", FAILED)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
