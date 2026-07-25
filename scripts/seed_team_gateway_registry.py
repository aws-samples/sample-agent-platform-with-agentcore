#!/usr/bin/env python3
"""Register an AgentCore Gateway in the platform registry and publish an agent
that exercises it.

This is what turns the gateway from infrastructure into a platform capability:

1. The gateway becomes one **MCP server** in the registry (kind ``url``), with
   an ``Authorization: Bearer {{user_token}}`` header. The placeholder is what
   is stored — the invocation pipeline substitutes the *calling user's* token
   per request, so every attachment of this entry carries the caller's own
   IdP identity into the gateway (and, through it, into the backend APIs).
   One gateway endpoint exposes all of its targets' tools.

2. A published agent (``team-access-tester``) attaches that MCP server, so the
   Debug console, channels, evals and plain HTTP callers can all invoke the
   same agent and get results that differ **by who is signed in** — no
   demo-only code path anywhere.

Idempotent: the MCP entry is created once (matched by name) and the agent is
re-published (version bump) on every run.

Run against the deployed platform with its env + AWS creds:

    PLATFORM_AWS_REGION=ap-northeast-1 \
    PLATFORM_DYNAMO_TABLE=agent-platform \
    PLATFORM_WORKSPACE_BUCKET=agent-platform-workspaces-<ACCOUNT_ID>-<REGION> \
    python3 scripts/seed_team_gateway_registry.py
"""

import argparse
import json
import os
import sys

import boto3

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from app.services.agent_service import agent_service  # noqa: E402
from app.services.ecosystem_service import ecosystem_service  # noqa: E402

SEED_USER = "seed"
SSM_PARAM = "/agent-platform/team-gateway"
MCP_NAME = "team-apis-gateway"
AGENT_NAME = "team-access-tester"

AGENT_SYSTEM = """You are an access-verification agent for an internal platform.

Your tools reach several team-scoped backend APIs through one AgentCore
Gateway. The gateway authenticates the end user who invoked you and carries
that identity to each backend, so **the set of tools you can successfully
call depends on who invoked you** — not on what you decide.

When asked to check access:

1. List the tools you have and group them by the team prefix in each name
   (for example `team-a___...`, `team-b___...`, `team-c___...`).
2. Attempt one read-only tool for EVERY team, even the ones you expect to
   fail. Never skip a call because you assume it will be refused.
3. Report a compact table: team | tool called | allowed or denied | the exact
   error message when denied.
4. If a call was denied, quote the error verbatim — it states which layer
   refused (the backend service itself, or the gateway's interceptor). Do not
   paraphrase or guess the reason.
5. Never invent data for a team whose call failed. A denial is a valid,
   expected result: report it as such.

Finish with one line stating which team the caller evidently belongs to,
based only on which calls succeeded."""

AGENT_DESCRIPTION = (
    "Verifies, per signed-in user, which team APIs are reachable through the "
    "AgentCore Gateway and reports where each denial was enforced"
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mcp-url", help="gateway MCP endpoint (default: from SSM)")
    parser.add_argument("--region", default=os.environ.get("PLATFORM_AWS_REGION"))
    args = parser.parse_args()

    mcp_url = args.mcp_url
    if not mcp_url:
        ssm = boto3.client("ssm", region_name=args.region)
        try:
            wiring = json.loads(ssm.get_parameter(Name=SSM_PARAM)["Parameter"]["Value"])
        except Exception as exc:  # noqa: BLE001
            print(f"could not read {SSM_PARAM}: {exc}", file=sys.stderr)
            print("deploy the gateway first (scripts/deploy_team_gateway.py)", file=sys.stderr)
            return 1
        mcp_url = wiring.get("mcp_url", "")
        teams = wiring.get("teams", [])
        print(f"gateway: {mcp_url}\ntargets: {', '.join(teams)}")
    if not mcp_url:
        print("gateway has no MCP endpoint", file=sys.stderr)
        return 1

    # ---------------------------- MCP registry ---------------------------
    existing = next(
        (m for m in ecosystem_service.list_mcp_servers() if m["name"] == MCP_NAME), None
    )
    if existing:
        print(f"MCP entry exists: {MCP_NAME} ({existing['id']})")
    else:
        entry = ecosystem_service.create_mcp_server(
            name=MCP_NAME,
            description=(
                "Team-scoped internal APIs behind one AgentCore Gateway. Forwards the "
                "caller's own identity; each backend (or the gateway interceptor) "
                "decides what that identity may do."
            ),
            kind="url",
            target=mcp_url,
            # stored as a placeholder — never a real credential
            headers={"Authorization": "Bearer {{user_token}}"},
        )
        print(f"MCP entry created: {MCP_NAME} ({entry['id']})")

    # --------------------------- published agent -------------------------
    agent = agent_service.publish(
        user=SEED_USER,
        name=AGENT_NAME,
        description=AGENT_DESCRIPTION,
        system_prompt=AGENT_SYSTEM,
        max_turns=12,
        mcp_server_names=[MCP_NAME],
        source="seed",
    )
    print(f"agent published: {agent['name']} v{agent['version']} (id {agent['id']})")
    print(
        "\nNext: sign in to the portal as different users and invoke "
        f"'agent: {AGENT_NAME}' from the Debug page — the same agent reports "
        "different reachable teams per identity."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
