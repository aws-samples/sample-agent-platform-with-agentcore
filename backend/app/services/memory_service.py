"""Memory: AgentCore Memory stores managed by the platform.

The platform exposes AgentCore Memory as a first-class resource: operators
create *stores* from the portal (control-plane ``CreateMemory``), and any
headless invocation can bind to a store — the kernel retrieves relevant
long-term records before the run and appends the exchange as an event after
it, giving agents recall **across** runtime sessions.

Stores are created with two built-in long-term strategies (semantic facts +
user preferences), both extracting into the namespace ``/users/{actorId}``
so retrieval needs only the actor ID. Short-term memory (raw events) is
available immediately; long-term records appear after AgentCore's async
extraction (typically well under a minute).

A default store (``platform-default``) is seeded lazily on first use so the
Memory page works out of the box. Creation takes a few minutes — status is
surfaced as-is (CREATING → ACTIVE).
"""

import logging
from datetime import datetime, timezone

import boto3
from botocore.config import Config

from app.config import settings

logger = logging.getLogger(__name__)

DEFAULT_STORE_NAME = "platform_default"
NAMESPACE_TEMPLATE = "/users/{actorId}"
EVENT_EXPIRY_DAYS = 90


class MemoryService:
    def __init__(self) -> None:
        cfg = Config(retries={"max_attempts": 2})
        self.control = boto3.client(
            "bedrock-agentcore-control", region_name=settings.aws_region, config=cfg
        )
        self.data = boto3.client(
            "bedrock-agentcore", region_name=settings.aws_region, config=cfg
        )

    # ------------------------------------------------------------- helpers

    @staticmethod
    def _to_public(m: dict) -> dict:
        return {
            "id": m.get("id", ""),
            "arn": m.get("arn", ""),
            "name": m.get("name", ""),
            "description": m.get("description", ""),
            "status": m.get("status", ""),
            "event_expiry_days": m.get("eventExpiryDuration", 0),
            "strategies": [
                {
                    "id": s.get("strategyId", ""),
                    "type": s.get("type", s.get("memoryStrategyType", "")),
                    "name": s.get("name", ""),
                }
                for s in m.get("strategies", []) or []
            ],
            "created_at": (
                m["createdAt"].astimezone(timezone.utc).isoformat()
                if isinstance(m.get("createdAt"), datetime)
                else str(m.get("createdAt", ""))
            ),
        }

    # ----------------------------------------------------------------- API

    def list_stores(self) -> list[dict]:
        stores: list[dict] = []
        paginator = self.control.get_paginator("list_memories")
        for page in paginator.paginate():
            for summary in page.get("memories", []):
                # ListMemories returns summaries; GetMemory fills in strategies
                try:
                    full = self.control.get_memory(memoryId=summary["id"])["memory"]
                except Exception:
                    full = summary
                stores.append(self._to_public(full))
        return sorted(stores, key=lambda s: s["created_at"], reverse=True)

    def ensure_default_store(self) -> dict | None:
        """Idempotently create the platform-default store. Returns the store
        (possibly still CREATING) or None if creation failed."""
        try:
            for store in self.list_stores():
                if store["name"] == DEFAULT_STORE_NAME:
                    return store
            return self.create_store(
                name=DEFAULT_STORE_NAME,
                description="Default long-term memory store seeded by the platform",
            )
        except Exception:
            logger.exception("ensure_default_store failed")
            return None

    def create_store(self, *, name: str, description: str = "") -> dict:
        resp = self.control.create_memory(
            name=name,
            description=description[:400] or None,
            eventExpiryDuration=EVENT_EXPIRY_DAYS,
            memoryStrategies=[
                {
                    "semanticMemoryStrategy": {
                        "name": "facts",
                        "description": "Semantic facts extracted from conversations",
                        "namespaces": [NAMESPACE_TEMPLATE],
                    }
                },
                {
                    "userPreferenceMemoryStrategy": {
                        "name": "preferences",
                        "description": "Stable user preferences",
                        "namespaces": [NAMESPACE_TEMPLATE],
                    }
                },
            ],
        )
        return self._to_public(resp["memory"])

    def get_store(self, memory_id: str) -> dict:
        return self._to_public(self.control.get_memory(memoryId=memory_id)["memory"])

    def delete_store(self, memory_id: str) -> None:
        self.control.delete_memory(memoryId=memory_id)

    # ------------------------------------------------------------ browsing

    def list_actors(self, memory_id: str) -> list[str]:
        resp = self.data.list_actors(memoryId=memory_id, maxResults=50)
        return [a.get("actorId", "") for a in resp.get("actorSummaries", [])]

    def list_events(self, memory_id: str, actor_id: str, limit: int = 20) -> list[dict]:
        """Recent raw events (short-term memory) for one actor, newest first."""
        sessions = self.data.list_sessions(
            memoryId=memory_id, actorId=actor_id, maxResults=20
        ).get("sessionSummaries", [])
        events: list[dict] = []
        for s in sessions:
            resp = self.data.list_events(
                memoryId=memory_id,
                actorId=actor_id,
                sessionId=s["sessionId"],
                maxResults=limit,
                includePayloads=True,
            )
            for ev in resp.get("events", []):
                texts = []
                for p in ev.get("payload", []) or []:
                    conv = p.get("conversational") or {}
                    text = (conv.get("content") or {}).get("text", "")
                    if text:
                        texts.append(f"{conv.get('role', '?')}: {text[:200]}")
                events.append(
                    {
                        "session_id": s["sessionId"],
                        "event_id": ev.get("eventId", ""),
                        "at": (
                            ev["eventTimestamp"].astimezone(timezone.utc).isoformat()
                            if isinstance(ev.get("eventTimestamp"), datetime)
                            else ""
                        ),
                        "messages": texts,
                    }
                )
        events.sort(key=lambda e: e["at"], reverse=True)
        return events[:limit]

    def retrieve_records(self, memory_id: str, actor_id: str, query: str, top_k: int = 8) -> list[dict]:
        """Long-term records (post-extraction) via semantic retrieval."""
        resp = self.data.retrieve_memory_records(
            memoryId=memory_id,
            namespace=NAMESPACE_TEMPLATE.replace("{actorId}", actor_id),
            searchCriteria={"searchQuery": query[:1000], "topK": top_k},
        )
        return [
            {
                "record_id": r.get("memoryRecordId", ""),
                "text": (r.get("content") or {}).get("text", "")[:500],
                "namespaces": r.get("namespaces", []),
                "score": float(r["score"]) if "score" in r else None,
            }
            for r in resp.get("memoryRecordSummaries", [])
        ]


memory_service = MemoryService()
