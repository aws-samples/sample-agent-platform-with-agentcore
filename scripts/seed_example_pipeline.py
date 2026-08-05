#!/usr/bin/env python3
"""Register the example pipeline and the one agent it targets.

Pipelines are data: the script in pipelines/*.workflow.mjs is stored on the
platform and executed by the workflow runner, and every agent() call in it
resolves to a *published agent* by name. So seeding a pipeline is two writes —
publish the agents, register the script — and both are idempotent (re-running
bumps versions rather than duplicating).

Run against a deployed platform with its env + your AWS credentials:

    PLATFORM_AWS_REGION=<region> \
    PLATFORM_DYNAMO_TABLE=agent-platform \
    PLATFORM_WORKSPACE_BUCKET=agent-platform-workspaces-<ACCOUNT_ID>-<REGION> \
    python3 scripts/seed_example_pipeline.py

This runs with your CLI identity, not a stack role: it needs dynamodb:PutItem
on the platform table. The example pipeline itself only reads and writes the
workspace bucket under feeds/, which the kernel roles already grant.

Copy this file alongside your own pipeline; the two functions below are the
whole pattern.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from app.services.agent_service import agent_service  # noqa: E402
from app.services.pipeline_service import pipeline_service  # noqa: E402

SEED_USER = "seed"

# The agents a pipeline targets are deliberately thin: one job, a JSON contract
# in the system prompt, and no discretion about what to work on. The pipeline
# script decides what gets called and with which input; the agent just answers.
#
# Two details that matter in practice:
#   - The kernel does not enforce structured output, so the contract has to be
#     stated in the prompt and the caller has to survive not getting it.
#   - max_turns has to cover the tool round-trips. This agent has no tools, so
#     a small budget is fine; an agent that calls one MCP tool needs ~8, since
#     a single tool round-trip costs 2-3 turns.
EXAMPLE_AGENTS = [
    {
        "name": "example-summarizer",
        "description": "Example pipeline · summarise one document, or reduce a batch of summaries",
        "system_prompt": (
            "You summarise text for a pipeline. Follow the user message exactly: "
            "it says whether you are summarising one document or reducing a batch, "
            "and which JSON keys to return. Keep concrete numbers, scales and dates "
            "verbatim — never replace a specific fact with a generality, and never "
            "invent one that is not in the input. Reply with exactly one JSON object "
            "and nothing else: no explanation, no markdown fences."
        ),
        # No tools: text in, JSON out. Tool-using agents need more headroom.
        "max_turns": 6,
        "mcp_server_names": [],
    },
]

PIPELINES = [
    (
        "example-digest.workflow.mjs",
        "example-digest",
        "Example pipeline: fan out one agent per input document, then reduce to a digest artifact",
    ),
]


def seed_agents() -> None:
    for a in EXAMPLE_AGENTS:
        r = agent_service.publish(user=SEED_USER, source="seed", **a)
        print(f"  published agent     {r['name']:<22} v{r['version']}  id={r['id']}")


def seed_pipelines() -> None:
    # Agents first: register() does not resolve agent names, so a pipeline whose
    # agents are missing registers fine and then fails at run time.
    for filename, name, description in PIPELINES:
        path = os.path.join(os.path.dirname(__file__), "..", "pipelines", filename)
        with open(path, encoding="utf-8") as f:
            script = f.read()
        p = pipeline_service.register(
            user=SEED_USER, name=name, description=description, script=script,
        )
        print(f"  registered pipeline {p['name']:<22} v{p['version']}  id={p['id']}  ({p['script_size']} bytes)")


if __name__ == "__main__":
    print("publishing example agents…")
    seed_agents()
    print("registering example pipeline…")
    seed_pipelines()
    print("done. Run it from the Workflow page, or:")
    print("  POST /api/v1/pipelines/example-digest/runs   {\"args\": {}}")
