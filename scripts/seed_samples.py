#!/usr/bin/env python3
"""Seed the Sample Agents catalogue from samples/ into a deployed platform.

The backend image does not carry samples/ (its build context is backend/),
so this runs where the repository is checked out. Two ways in:

  --portal (the CI way): through the portal API as an administrator, the way
  the acceptance checks address an environment (scripts/qa_env.py: PORTAL_URL,
  QA_TEST_USERS_SECRET, AWS_REGION, QA_ENV). The verify build runs this before
  the deploy-tier checks, so the catalogue is seeded by the same role that
  verifies it, and no CI role needs DynamoDB writes.

      python3 scripts/seed_samples.py --portal [--only <id>] [--dry-run]

  direct (the operator way): with the backend's settings in the environment
  and AWS credentials that may write the platform table.

      PLATFORM_AWS_REGION=<region> PLATFORM_DYNAMO_TABLE=agent-platform[-staging] \\
      PLATFORM_WORKSPACE_BUCKET=<bucket> \\
      PLATFORM_SAMPLE_RUNTIME_ARNS='{"investment-assistant": "<arn>"}' \\
      PLATFORM_SAMPLE_TOOLS_GATEWAY_URL=<gateway mcp url> \\
      PLATFORM_SDK_RUNTIME_ARNS='{"V1": "<sdk runtime arn>"}' \\
      PLATFORM_INTERACTIVE_RUNTIME_ARNS='{"V1": "<interactive runtime arn>"}' \\
      PLATFORM_DEFAULT_PLATFORM_VERSION=V1 \\
      python3 scripts/seed_samples.py [--only <id>] [--dry-run]
  The runtime ARN maps are what the backend pod carries; without them a
  sample whose agent.yaml names a platform_version is refused by
  resolve_platform_version ("this deployment offers a single runtime").

Either way, per sample: the sample-tools gateway is registered in the
ecosystem as the MCP entry the samples attach (if not already), the catalogue
item (sample.yaml + agent.yaml with prompt.md inlined + README.md +
eval.jsonl) is written, and the sample's shared instance is published as the
seed user on the sample's runtime so "Try" has something to call. All three
are idempotent: a rerun refreshes the item and bumps the shared instance's
version, its id stays.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLES = os.path.join(ROOT, "samples", "agents")


def read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def load_sample(sample_id: str) -> tuple[dict, dict, str, list[dict]]:
    d = os.path.join(SAMPLES, sample_id)
    sample = yaml.safe_load(read(os.path.join(d, "sample.yaml")))
    agent = yaml.safe_load(read(os.path.join(d, "agent.yaml")))
    agent["system_prompt"] = read(os.path.join(d, "prompt.md")).strip()
    readme = read(os.path.join(d, "README.md"))
    cases = [json.loads(ln) for ln in read(os.path.join(d, sample.get("eval", "eval.jsonl"))).splitlines() if ln.strip()]
    return sample, agent, readme, cases


# ------------------------------------------------------------- portal path

def seed_via_portal(ids: list[str], dry_run: bool) -> int:
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    import qa_env  # noqa: E402

    qa_env.check_same_environment()
    token = qa_env.token_for("admin")
    base = qa_env.portal()
    print(f"seeding {len(ids)} sample(s) through {base} as admin")
    rc = 0
    # the gateway entry: read the facts the build exported, register if absent
    gw_url = os.environ.get("SAMPLE_TOOLS_GATEWAY_URL", "")
    st, mcp = qa_env.api(token, "GET", "/api/v1/ecosystem/mcp-servers")
    names = {m.get("name"): m for m in (mcp if isinstance(mcp, list) else [])}
    for sid in ids:
        sample, agent, readme, cases = load_sample(sid)
        entry = str(sample["mcp_server"])
        if entry not in names:
            if not gw_url:
                print(f"  ! {entry} is not registered and SAMPLE_TOOLS_GATEWAY_URL is empty: the sample will not be cloneable")
            elif dry_run:
                print(f"  + would register MCP entry {entry} -> {gw_url}")
            else:
                st, body = qa_env.api(token, "POST", "/api/v1/ecosystem/mcp-servers", {
                    "name": entry, "kind": "agentcore-gateway", "target": gw_url,
                    "description": "Sample Agents tool gateway (AgentCore Gateway, SigV4 inbound, Cedar policy per sample runtime)",
                })
                print(f"  {'+' if st == 200 else '!'} register {entry}: {st} {str(body)[:80]}")
                names[entry] = body
        if dry_run:
            print(f"  would seed {sid}: {sample.get('title', '')}")
            continue
        st, body = qa_env.api(token, "PUT", f"/api/v1/samples/{sid}", {
            "sample": sample, "agent": agent, "readme": readme, "eval_cases": cases, "publish_shared": True,
        }, timeout=120)
        if st != 200:
            print(f"  ! {sid}: {st} {str(body)[:200]}")
            rc = 1
            continue
        status = body.get("status", {})
        shared = body.get("shared_instance") or {}
        print(f"  seeded {sid}: cloneable={status.get('cloneable')} shared={shared.get('name', '-')} v{shared.get('version', '-')}"
              + ("" if status.get("cloneable") else f" blocking={status.get('blocking')}"))
    return rc


# ------------------------------------------------------------- direct path

def seed_direct(ids: list[str], dry_run: bool) -> int:
    sys.path.insert(0, os.path.join(ROOT, "backend"))
    from app.config import settings  # noqa: E402
    from app.services.agent_service import agent_service  # noqa: E402
    from app.services.ecosystem_service import ecosystem_service  # noqa: E402
    from app.services.samples_service import SEED_USER, samples_service  # noqa: E402

    print(f"seeding {len(ids)} sample(s) into {settings.dynamo_table} ({settings.aws_region}); runtimes: {sorted(settings.sample_runtime_arns)}")
    existing = {m["name"]: m for m in ecosystem_service.list_mcp_servers()}
    for sid in ids:
        sample, agent, readme, cases = load_sample(sid)
        entry = str(sample["mcp_server"])
        url = settings.sample_tools_gateway_url
        if entry not in existing:
            if not url:
                print(f"  ! PLATFORM_SAMPLE_TOOLS_GATEWAY_URL is empty: not registering {entry}")
            elif not dry_run:
                ecosystem_service.create_mcp_server(entry, "Sample Agents tool gateway (AgentCore Gateway, SigV4 inbound, Cedar policy per sample runtime)", "agentcore-gateway", url)
                print(f"  + MCP entry {entry} -> {url}")
        elif existing[entry].get("target") != url and url:
            print(f"  ! MCP entry {entry} points at {existing[entry].get('target')}, facts say {url}")
        print(f"  catalogue {sid}: {sample.get('title', '')}")
        if dry_run:
            continue
        item = samples_service.upsert(sample, agent=agent, readme=readme, eval_cases=cases)
        status = samples_service.status(item)
        if not status["cloneable"]:
            print(f"  ! {sid} is not cloneable here: {'; '.join(status['blocking'])} (item written; shared instance skipped)")
            continue
        ref = f"sample:{sid}"
        published = agent_service.publish(
            user=SEED_USER, name=str(agent.get("name", sid)), description=str(agent.get("description", "")),
            system_prompt=agent["system_prompt"], max_turns=int(agent.get("max_turns", 8)),
            mcp_server_names=[str(x) for x in agent.get("mcp_server_names") or []],
            skill_names=[str(x) for x in agent.get("skill_names") or []],
            platform_version=str(agent.get("platform_version", "")), source=ref, runtime=ref,
        )
        samples_service.set_shared_agent(sid, published["id"])
        print(f"  shared instance {published['name']} v{published['version']} id={published['id']} on {ref}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="seed one sample id")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--portal", action="store_true", help="seed through the portal API as the environment's admin test user (qa_env)")
    args = ap.parse_args()
    ids = [args.only] if args.only else sorted(d for d in os.listdir(SAMPLES) if os.path.isdir(os.path.join(SAMPLES, d)))
    rc = seed_via_portal(ids, args.dry_run) if args.portal else seed_direct(ids, args.dry_run)
    print("done" if rc == 0 else "done with errors")
    return rc


if __name__ == "__main__":
    sys.exit(main())
