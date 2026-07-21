"""Ecosystem registry: MCP servers and skill packages.

Registry entries live in the platform DynamoDB table (PK=ECOSYSTEM). Skill
content (SKILL.md) is stored in the workspace bucket under ``skills/{id}/`` so
runtime containers — which already have bucket access — can sync packages
straight into a session workspace.

On first use the registry seeds itself with the platform-hosted MCP tools
runtime (if configured) and two sample skills, so a fresh deployment has a
working catalog out of the box.
"""

import logging
import uuid
from datetime import datetime, timezone

import boto3

from app.config import settings
from app.services.seed_data import BUILTIN_TOOLS, SAMPLE_SKILLS

logger = logging.getLogger(__name__)

PK = "ECOSYSTEM"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class EcosystemService:
    def __init__(self) -> None:
        dynamodb = boto3.resource("dynamodb", region_name=settings.aws_region)
        self.table = dynamodb.Table(settings.dynamo_table)
        self.s3 = boto3.client("s3", region_name=settings.aws_region)
        self._seeded = False

    # ------------------------------------------------------------- seeding

    def _ensure_seeded(self) -> None:
        # Seed each category independently and idempotently: a partial
        # failure (e.g. missing S3 permission for skills) must not leave the
        # other category permanently unseeded.
        if self._seeded:
            return
        try:
            # Name-based idempotency (not "category empty") so new seed
            # entries still land in registries populated by older versions.
            existing_mcp = {i.get("name") for i in self._query_prefix("MCP#")}
            if "platform-tools" not in existing_mcp and settings.mcp_tools_runtime_arn:
                self._put_mcp(
                    name="platform-tools",
                    description="Demo internal tools (directory / KB / ticketing) hosted on AgentCore Runtime",
                    kind="agentcore-runtime",
                    target=settings.mcp_tools_runtime_arn,
                    builtin=True,
                )
            for name, description in BUILTIN_TOOLS.items():
                if name not in existing_mcp:
                    self._put_mcp(
                        name=name,
                        description=description,
                        kind="builtin",
                        target=name,
                        builtin=True,
                    )
            if not self._query_prefix("SKILL#"):
                for name, spec in SAMPLE_SKILLS.items():
                    self._put_skill(name, spec["description"], spec["skill_md"], builtin=True)
            self._seeded = True
            logger.info("ecosystem registry seeded")
        except Exception as e:  # never block the API on seeding; retry next call
            logger.warning("ecosystem seed failed: %s", e)

    # -------------------------------------------------------------- helpers

    def _query_prefix(self, prefix: str) -> list[dict]:
        resp = self.table.query(
            KeyConditionExpression="PK = :pk AND begins_with(SK, :p)",
            ExpressionAttributeValues={":pk": PK, ":p": prefix},
        )
        return resp.get("Items", [])

    def _put_mcp(self, name, description, kind, target, builtin=False) -> dict:
        item = {
            "PK": PK,
            "SK": f"MCP#{uuid.uuid4().hex[:12]}",
            "name": name,
            "description": description,
            "kind": kind,  # agentcore-runtime | url | builtin
            "target": target,  # runtime ARN | http(s) URL
            "builtin": builtin,
            "created_at": _now(),
        }
        self.table.put_item(Item=item)
        return item

    def _put_skill(self, name, description, skill_md, builtin=False) -> dict:
        skill_id = uuid.uuid4().hex[:12]
        key = f"skills/{skill_id}/SKILL.md"
        self.s3.put_object(
            Bucket=settings.workspace_bucket, Key=key, Body=skill_md.encode()
        )
        item = {
            "PK": PK,
            "SK": f"SKILL#{skill_id}",
            "name": name,
            "description": description,
            "s3_prefix": f"skills/{skill_id}/",
            "builtin": builtin,
            "created_at": _now(),
        }
        self.table.put_item(Item=item)
        return item

    @staticmethod
    def _to_public(item: dict) -> dict:
        kind, _, obj_id = item["SK"].partition("#")
        return {
            "id": obj_id,
            "type": kind.lower(),
            "name": item.get("name", ""),
            "description": item.get("description", ""),
            "kind": item.get("kind", ""),
            "target": item.get("target", ""),
            "s3_prefix": item.get("s3_prefix", ""),
            "builtin": bool(item.get("builtin")),
            "created_at": item.get("created_at", ""),
        }

    # ----------------------------------------------------------------- API

    def list_mcp_servers(self) -> list[dict]:
        self._ensure_seeded()
        return sorted(
            (self._to_public(i) for i in self._query_prefix("MCP#")),
            key=lambda x: x["created_at"],
        )

    def list_skills(self) -> list[dict]:
        self._ensure_seeded()
        return sorted(
            (self._to_public(i) for i in self._query_prefix("SKILL#")),
            key=lambda x: x["created_at"],
        )

    def create_mcp_server(self, name, description, kind, target) -> dict:
        return self._to_public(self._put_mcp(name, description, kind, target))

    def create_skill(self, name, description, skill_md) -> dict:
        return self._to_public(self._put_skill(name, description, skill_md))

    def delete(self, kind: str, obj_id: str) -> bool:
        sk = f"{kind.upper()}#{obj_id}"
        resp = self.table.get_item(Key={"PK": PK, "SK": sk})
        item = resp.get("Item")
        if not item:
            return False
        if item.get("builtin"):
            raise ValueError("built-in entries cannot be deleted")
        self.table.delete_item(Key={"PK": PK, "SK": sk})
        return True

    def resolve_session_config(
        self, mcp_server_ids: list[str], skill_ids: list[str]
    ) -> dict:
        """Resolve registry IDs into the config the kernel applies at warmup."""
        self._ensure_seeded()
        mcp = {self._to_public(i)["id"]: self._to_public(i) for i in self._query_prefix("MCP#")}
        skills = {self._to_public(i)["id"]: self._to_public(i) for i in self._query_prefix("SKILL#")}
        return {
            "mcp_servers": [
                {"name": mcp[i]["name"], "kind": mcp[i]["kind"], "target": mcp[i]["target"]}
                for i in mcp_server_ids
                if i in mcp
            ],
            "skills": [
                {
                    "name": skills[i]["name"],
                    "s3_uri": f"s3://{settings.workspace_bucket}/{skills[i]['s3_prefix']}",
                }
                for i in skill_ids
                if i in skills
            ],
        }


ecosystem_service = EcosystemService()
