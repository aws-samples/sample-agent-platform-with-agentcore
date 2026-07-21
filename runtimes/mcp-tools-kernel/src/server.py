"""Demo MCP server hosted on Amazon Bedrock AgentCore Runtime.

AgentCore's MCP protocol contract: the container serves stateless
streamable-HTTP MCP at 0.0.0.0:8000/mcp. The platform injects an
`Mcp-Session-Id` header and handles auth (SigV4/OAuth) before requests reach
this process, so the server itself carries no auth logic.

The tools below are a MOCK "internal tools" facade — the kind of corporate
systems (directory, knowledge base, ticketing) an enterprise would expose to
its agents through MCP. Replace them with real integrations.
"""

from datetime import datetime, timezone
from uuid import uuid4

from mcp.server.fastmcp import FastMCP

# Binding 0.0.0.0:8000 is required by the AgentCore MCP protocol contract —
# the platform routes traffic to the container port; there is no other network
# path in (VPC egress SG + microVM isolation front this).
mcp = FastMCP(name="platform-tools", host="0.0.0.0", port=8000, stateless_http=True)  # nosec B104

_EMPLOYEES = {
    "alice": {"name": "Alice Chen", "team": "Data Platform", "role": "Engineering Manager", "location": "Singapore", "email": "alice@example.com"},
    "bob": {"name": "Bob Martins", "team": "Risk & Compliance", "role": "Senior Analyst", "location": "Dublin", "email": "bob@example.com"},
    "carol": {"name": "Carol Ito", "team": "Trading Infrastructure", "role": "Staff Engineer", "location": "Tokyo", "email": "carol@example.com"},
}

_KB = [
    {"id": "KB-101", "title": "How to request a new AgentCore runtime", "summary": "File a platform ticket with the image URI and expected traffic; the platform team provisions the runtime and registers it in the catalog."},
    {"id": "KB-204", "title": "LLM gateway budgets and allow-lists", "summary": "Each team gets a scoped virtual key on the LLM gateway. Budgets reset monthly; model allow-lists are managed by the platform team."},
    {"id": "KB-317", "title": "Workspace data retention policy", "summary": "Interactive workspace data in S3 is retained for 90 days after the last session activity, then archived."},
]


@mcp.tool()
def lookup_employee(alias: str) -> dict:
    """Look up an employee in the (mock) corporate directory by alias."""
    emp = _EMPLOYEES.get(alias.lower().strip())
    if not emp:
        return {"found": False, "known_aliases": sorted(_EMPLOYEES)}
    return {"found": True, **emp}


@mcp.tool()
def search_knowledge_base(query: str) -> list[dict]:
    """Search the (mock) internal knowledge base. Returns matching articles."""
    q = query.lower()
    hits = [a for a in _KB if any(w in (a["title"] + a["summary"]).lower() for w in q.split())]
    return hits or [{"id": None, "title": "no match", "summary": f"No KB articles matched: {query}"}]


@mcp.tool()
def create_ticket(title: str, body: str, priority: str = "normal") -> dict:
    """Create a (mock) internal ticket. Returns the ticket ID and status."""
    return {
        "ticket_id": f"TKT-{uuid4().hex[:8].upper()}",
        "title": title,
        "priority": priority,
        "status": "open",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "note": "mock ticket — wire this tool to your real ticketing system",
    }


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
