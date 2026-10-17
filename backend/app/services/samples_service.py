"""Sample Agents: the catalogue of reference agents a user can read, try and clone.

A sample is content (samples/agents/<id>/ in the repository: sample.yaml,
agent.yaml, prompt.md, eval.jsonl, README.md) plus infrastructure the two
Terraform roots create (its own runtime and execution role; the tool packages
on the sample-tools gateway). The backend image does not carry the content,
so the deploy seeds it here (scripts/seed_samples.py, through this service's
``upsert``), one DynamoDB item per sample under PK=SAMPLE, and publishes the
sample's shared instance as a regular agent owned by the seed user.

What this service decides:

  - **status**: whether the sample can be cloned here. Its runtime must be
    deployed (settings.sample_runtime_arns), its MCP entry registered, and
    each required tool present on the gateway (tool names are what the
    gateway exposes: ``<target>___<op>``). An optional tool that is absent is
    reported, not blocking.
  - **clone**: publishes a copy under the caller's name with the sample's
    runtime and MCP attachment fixed (source = runtime = ``sample:<id>``).
    agent_service refuses any other way of putting an agent onto a sample
    runtime, so the identity boundary cannot be reached from the Publish page.
  - **try**: invokes the shared instance through the governed pipeline, as
    the caller, so the quota and the ledger see it like any other call.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import boto3

from app.config import SAMPLE_RUNTIME_PREFIX, settings
from app.services.agent_service import agent_service
from app.services.ecosystem_service import ecosystem_service
from app.services.dynamo import query_all

logger = logging.getLogger(__name__)

PK = "SAMPLE"
SEED_USER = "seed"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SamplesService:
    def __init__(self) -> None:
        dynamodb = boto3.resource("dynamodb", region_name=settings.aws_region)
        self.table = dynamodb.Table(settings.dynamo_table)
        self._gateway_tools_cache: tuple[float, set[str]] | None = None

    # ------------------------------------------------------------- storage

    @staticmethod
    def _to_public(item: dict) -> dict:
        return {
            "id": item["SK"].partition("#")[2],
            "title": item.get("title", ""),
            "summary": item.get("summary", ""),
            "patterns": item.get("patterns", []),
            "industries": item.get("industries", []),
            "runtime": item.get("runtime", ""),
            "requires_tools": item.get("requires_tools", []),
            "mcp_server": item.get("mcp_server", ""),
            "try_prompt": item.get("try_prompt", ""),
            "readme": item.get("readme", ""),
            "design_doc": item.get("design_doc", ""),
            "agent": item.get("agent", {}),
            "eval_cases": item.get("eval_cases", []),
            "shared_agent_id": item.get("shared_agent_id", ""),
            "updated_at": item.get("updated_at", ""),
        }

    def list_samples(self) -> list[dict]:
        items = query_all(
            self.table,
            KeyConditionExpression="PK = :pk AND begins_with(SK, :p)",
            ExpressionAttributeValues={":pk": PK, ":p": "SAMPLE#"},
        )
        return sorted((self._to_public(i) for i in items), key=lambda s: s["id"])

    def get_sample(self, sample_id: str) -> dict | None:
        resp = self.table.get_item(Key={"PK": PK, "SK": f"SAMPLE#{sample_id}"})
        item = resp.get("Item")
        return self._to_public(item) if item else None

    def upsert(self, sample: dict, *, agent: dict, readme: str, eval_cases: list[dict]) -> dict:
        """Seed one sample (scripts/seed_samples.py). ``sample`` is sample.yaml,
        ``agent`` is agent.yaml with the prompt inlined; both validated by
        samples.lint before they get here, lightly re-checked anyway."""
        sample_id = str(sample["id"])
        if not sample_id.replace("-", "").isalnum() or sample_id != sample_id.lower():
            raise ValueError("sample id must be lowercase letters, digits, hyphens")
        if str(sample.get("runtime", "")) != sample_id:
            raise ValueError("a sample runs on its own runtime (runtime == id)")
        requires = []
        for req in sample.get("requires_tools") or []:
            if isinstance(req, str):
                req = {"id": req}
            requires.append({"id": str(req["id"]), "optional": bool(req.get("optional", False))})
        item = {
            "PK": PK,
            "SK": f"SAMPLE#{sample_id}",
            "title": str(sample.get("title", ""))[:160],
            "summary": str(sample.get("summary", ""))[:1200],
            "patterns": [str(p) for p in sample.get("patterns") or []][:10],
            "industries": [str(p) for p in sample.get("industries") or []][:10],
            "runtime": sample_id,
            "requires_tools": requires,
            "mcp_server": str(sample.get("mcp_server", "")),
            "try_prompt": str(sample.get("try_prompt", ""))[:1000],
            "readme": readme[:60_000],
            "design_doc": str(sample.get("design_doc", "")),
            "agent": {
                "name": str(agent.get("name", sample_id)),
                "description": str(agent.get("description", ""))[:400],
                "system_prompt": str(agent.get("system_prompt", ""))[:20_000],
                "max_turns": int(agent.get("max_turns", 8)),
                "mcp_server_names": [str(x) for x in agent.get("mcp_server_names") or []],
                "skill_names": [str(x) for x in agent.get("skill_names") or []],
                "platform_version": str(agent.get("platform_version", "")),
            },
            "eval_cases": [
                {"prompt": str(c.get("prompt", ""))[:2000], "expected": str(c.get("expected", ""))[:1000],
                 "tags": [str(t) for t in c.get("tags") or []]}
                for c in eval_cases[:20]
            ],
            "updated_at": _now(),
        }
        existing = self.table.get_item(Key={"PK": PK, "SK": item["SK"]}).get("Item") or {}
        if existing.get("shared_agent_id"):
            item["shared_agent_id"] = existing["shared_agent_id"]
        self.table.put_item(Item=item)
        return self._to_public(item)

    def set_shared_agent(self, sample_id: str, agent_id: str) -> None:
        self.table.update_item(
            Key={"PK": PK, "SK": f"SAMPLE#{sample_id}"},
            UpdateExpression="SET shared_agent_id = :a, updated_at = :t",
            ExpressionAttributeValues={":a": agent_id, ":t": _now()},
        )

    # -------------------------------------------------------------- status

    def _gateway_tools(self) -> set[str]:
        """Tool names the sample-tools gateway exposes (``<target>___<op>``),
        read through the gateway control API; cached briefly. Empty when the
        feature is not deployed or the listing fails (then every required
        tool reads as missing, which is the honest answer)."""
        import time

        now = time.monotonic()
        if self._gateway_tools_cache and self._gateway_tools_cache[0] > now:
            return self._gateway_tools_cache[1]
        names: set[str] | None = set()
        url = settings.sample_tools_gateway_url
        if url:
            try:
                control = boto3.client("bedrock-agentcore-control", region_name=settings.aws_region)
                # the gateway id is the host's first label
                gateway_id = url.split("//", 1)[-1].split(".", 1)[0]
                paginator = control.get_paginator("list_gateway_targets")
                for page in paginator.paginate(gatewayIdentifier=gateway_id):
                    for t in page.get("items", []):
                        if t.get("status") == "READY":
                            names.add(t["name"])
            except Exception as e:  # noqa: BLE001 - status is advisory
                # No listing (no grant, throttled, gateway gone): the tool
                # state is unknown, which must not read as "every tool is
                # missing" and lock every sample. None = unknown; status()
                # reports the tools as unverified and does not block on them.
                logger.warning("sample-tools gateway target listing failed: %s", e)
                names = None
        self._gateway_tools_cache = (now + 60, names)
        return names

    def status(self, sample: dict) -> dict:
        """Can this sample be cloned here, and what is missing if not."""
        runtime_ok = sample["runtime"] in settings.sample_runtime_arns
        mcp_names = {m["name"] for m in ecosystem_service.list_mcp_servers()}
        mcp_ok = sample["mcp_server"] in mcp_names
        targets = self._gateway_tools()
        tools = []
        blocking = []
        for req in sample["requires_tools"]:
            present = None if targets is None else (req["id"] in targets)
            tools.append({"id": req["id"], "optional": req["optional"], "deployed": present})
            if present is False and not req["optional"]:
                blocking.append(f"tool {req['id']} is not on the sample-tools gateway")
        if not runtime_ok:
            blocking.append(f"runtime for sample {sample['runtime']} is not deployed")
        if not mcp_ok:
            blocking.append(f"MCP entry {sample['mcp_server']} is not registered")
        return {
            "cloneable": not blocking,
            "runtime_deployed": runtime_ok,
            "mcp_registered": mcp_ok,
            "tools": tools,
            # None when the gateway could not be listed: deployed is unknown, not false
            "tools_verified": targets is not None,
            "blocking": blocking,
            "shared_agent_id": sample.get("shared_agent_id", ""),
        }

    # --------------------------------------------------------------- clone

    def clone(self, *, user, sample: dict, name: str = "") -> dict:
        """Publish a copy of the sample under the caller's name, on the
        sample's runtime, with the sample's MCP attachment. Nothing else is
        changed at clone time; the user edits the published agent after."""
        st = self.status(sample)
        if not st["cloneable"]:
            raise ValueError("sample is not deployable here: " + "; ".join(st["blocking"]))
        a = sample["agent"]
        clone_name = name or f"{sample['id']}-{str(user).lower()}"
        clone_name = "".join(ch if (ch.isalnum() or ch in "-_") else "-" for ch in clone_name)[:80]
        ref = f"{SAMPLE_RUNTIME_PREFIX}{sample['id']}"
        return agent_service.publish(
            user=user,
            name=clone_name,
            description=a.get("description", ""),
            system_prompt=a.get("system_prompt", ""),
            max_turns=int(a.get("max_turns", 8)),
            mcp_server_names=list(a.get("mcp_server_names") or []),
            skill_names=list(a.get("skill_names") or []),
            platform_version=a.get("platform_version", ""),
            source=ref,
            runtime=ref,
        )


samples_service = SamplesService()
