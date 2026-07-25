"""Published agents: the self-service publish flow.

A *published agent* is a versioned configuration — system prompt, tool
attachments (MCP servers / skills / built-in tools), memory binding, turn
budget — served by the shared headless kernel. Publishing is config-only:
no image build, no new runtime, instant rollout and rollback. Consumers
(Debug console, channels, schedules, eval suites, plain API calls) target an
agent by ID and get the same `/invocations`-shaped answer as the raw kernel.

The self-service path reads an ``agent.yaml`` manifest straight out of a Dev
Workbench session's S3 workspace, so the loop is: iterate interactively in
the cloud workspace → drop a manifest → publish → invoke from anywhere.

Manifest example (``/workspace/agent.yaml``)::

    name: support-triage
    description: Classifies inbound tickets
    system_prompt: |
      You triage support tickets...
    max_turns: 8
    mcp_servers: [platform-tools, code-interpreter]
    skills: [code-review-checklist]

Republishing the same name bumps the version and keeps a compact history.
"""

import logging
import uuid
from datetime import datetime, timezone

import boto3
import yaml

from app.config import settings
from app.services.ecosystem_service import ecosystem_service

logger = logging.getLogger(__name__)

PK = "AGENT"

MANIFEST_CANDIDATES = ["agent.yaml", "agent.yml", "agent.json"]
MAX_HISTORY = 10


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class AgentService:
    def __init__(self) -> None:
        dynamodb = boto3.resource("dynamodb", region_name=settings.aws_region)
        self.table = dynamodb.Table(settings.dynamo_table)
        self.s3 = boto3.client("s3", region_name=settings.aws_region)

    # ------------------------------------------------------------- helpers

    @staticmethod
    def _to_public(item: dict) -> dict:
        return {
            "id": item["SK"].partition("#")[2],
            "name": item.get("name", ""),
            "description": item.get("description", ""),
            "system_prompt": item.get("system_prompt", ""),
            "max_turns": int(item.get("max_turns", 10)),
            "mcp_server_names": item.get("mcp_server_names", []),
            "skill_names": item.get("skill_names", []),
            "memory_id": item.get("memory_id", ""),
            "version": int(item.get("version", 1)),
            "source": item.get("source", "manual"),
            "created_by": item.get("created_by", ""),
            "created_at": item.get("created_at", ""),
            "updated_at": item.get("updated_at", ""),
            "history": item.get("history", []),
        }

    def _resolve_names(self, mcp_names: list[str], skill_names: list[str]) -> dict:
        """Translate registry *names* into warmup/invoke configs.

        Manifests reference tools by name (readable, survives re-seeding);
        unknown names fail the publish so a typo cannot ship silently.
        """
        mcp_by_name = {m["name"]: m for m in ecosystem_service.list_mcp_servers()}
        skills_by_name = {s["name"]: s for s in ecosystem_service.list_skills()}
        missing = [n for n in mcp_names if n not in mcp_by_name] + [
            n for n in skill_names if n not in skills_by_name
        ]
        if missing:
            raise ValueError(f"unknown registry entries: {', '.join(missing)}")
        return {
            "mcp_servers": [
                {
                    "name": mcp_by_name[n]["name"],
                    "kind": mcp_by_name[n]["kind"],
                    "target": mcp_by_name[n]["target"],
                    **(
                        {"headers": mcp_by_name[n]["headers"]}
                        if mcp_by_name[n].get("headers")
                        else {}
                    ),
                }
                for n in mcp_names
            ],
            "skills": [
                {
                    "name": skills_by_name[n]["name"],
                    "s3_uri": f"s3://{settings.workspace_bucket}/{skills_by_name[n]['s3_prefix']}",
                }
                for n in skill_names
            ],
        }

    # ----------------------------------------------------------------- API

    def list_agents(self) -> list[dict]:
        resp = self.table.query(
            KeyConditionExpression="PK = :pk AND begins_with(SK, :p)",
            ExpressionAttributeValues={":pk": PK, ":p": "AGENT#"},
        )
        return sorted(
            (self._to_public(i) for i in resp.get("Items", [])),
            key=lambda a: a["updated_at"],
            reverse=True,
        )

    def get_agent(self, agent_id: str) -> dict | None:
        resp = self.table.get_item(Key={"PK": PK, "SK": f"AGENT#{agent_id}"})
        item = resp.get("Item")
        return self._to_public(item) if item else None

    def publish(
        self,
        *,
        user: str,
        name: str,
        description: str = "",
        system_prompt: str = "",
        max_turns: int = 10,
        mcp_server_names: list[str] | None = None,
        skill_names: list[str] | None = None,
        memory_id: str = "",
        source: str = "manual",
    ) -> dict:
        """Create or re-publish (version bump) an agent by name."""
        if not name or not name.replace("-", "").replace("_", "").isalnum():
            raise ValueError("agent name must be alphanumeric with - or _")
        # validate attachments up front
        self._resolve_names(mcp_server_names or [], skill_names or [])

        existing = next((a for a in self.list_agents() if a["name"] == name), None)
        now = _now()
        if existing:
            agent_id = existing["id"]
            version = existing["version"] + 1
            history = ([{"version": existing["version"], "at": existing["updated_at"], "by": existing["created_by"]}]
                       + list(existing.get("history", [])))[:MAX_HISTORY]
            created_at = existing["created_at"]
        else:
            agent_id = uuid.uuid4().hex[:12]
            version, history, created_at = 1, [], now

        item = {
            "PK": PK,
            "SK": f"AGENT#{agent_id}",
            "name": name,
            "description": description[:400],
            "system_prompt": system_prompt[:20_000],
            "max_turns": max(1, min(int(max_turns), 50)),
            "mcp_server_names": mcp_server_names or [],
            "skill_names": skill_names or [],
            "memory_id": memory_id,
            "version": version,
            "source": source,
            "created_by": user,
            "created_at": created_at,
            "updated_at": now,
            "history": history,
        }
        self.table.put_item(Item=item)
        return self._to_public(item)

    def publish_from_workspace(self, *, user: str, runtime_session_id: str) -> dict:
        """Self-service publish: read the agent manifest from a session's S3
        workspace and publish it."""
        prefix = f"{settings.workspace_prefix}/{runtime_session_id}/"
        raw = name_found = None
        for candidate in MANIFEST_CANDIDATES:
            try:
                resp = self.s3.get_object(
                    Bucket=settings.workspace_bucket, Key=f"{prefix}{candidate}"
                )
                raw = resp["Body"].read(64 * 1024).decode("utf-8", errors="replace")
                name_found = candidate
                break
            except self.s3.exceptions.NoSuchKey:
                continue
        if raw is None:
            raise FileNotFoundError(
                "no agent manifest in the session workspace — create "
                f"{' or '.join(MANIFEST_CANDIDATES)} in /workspace first"
            )

        manifest = yaml.safe_load(raw)  # JSON is a YAML subset; one parser covers both
        if not isinstance(manifest, dict) or not manifest.get("name"):
            raise ValueError(f"{name_found}: manifest must be a mapping with a 'name' field")

        return self.publish(
            user=user,
            name=str(manifest["name"]),
            description=str(manifest.get("description", "")),
            system_prompt=str(manifest.get("system_prompt", "")),
            max_turns=int(manifest.get("max_turns", 10)),
            mcp_server_names=[str(x) for x in manifest.get("mcp_servers", []) or []],
            skill_names=[str(x) for x in manifest.get("skills", []) or []],
            memory_id=str(manifest.get("memory_id", "")),
            source=f"workspace:{runtime_session_id[:16]}",
        )

    def delete_agent(self, agent_id: str) -> bool:
        resp = self.table.get_item(Key={"PK": PK, "SK": f"AGENT#{agent_id}"})
        if not resp.get("Item"):
            return False
        self.table.delete_item(Key={"PK": PK, "SK": f"AGENT#{agent_id}"})
        return True

    def resolve_invoke_config(self, agent_id: str) -> dict:
        """The invocation pipeline calls this to expand an agent into the
        kernel payload pieces (system prompt, tool configs, memory binding)."""
        agent = self.get_agent(agent_id)
        if not agent:
            raise KeyError(f"agent {agent_id} not found")
        cfg = self._resolve_names(agent["mcp_server_names"], agent["skill_names"])
        return {
            "label": f"agent:{agent['name']}",
            "system_prompt": agent["system_prompt"],
            "max_turns": agent["max_turns"],
            "memory_id": agent["memory_id"],
            **cfg,
        }


agent_service = AgentService()
