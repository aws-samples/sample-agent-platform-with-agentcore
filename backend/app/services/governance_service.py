"""Governance: platform usage policy + quota enforcement.

A single policy item (``PK=GOV, SK=POLICY``) holds the platform-wide knobs.
Daily usage is tracked with atomic DynamoDB counters — one item per day for
the platform total and one per (day, user) pair — so enforcement is a cheap
conditional read-modify-write with no scanning.

Model-level governance (allow-lists, budgets per team) belongs in the LLM
gateway (LiteLLM); this layer governs *platform* invocations.
"""

import logging
from datetime import datetime, timezone

import boto3

from app.config import settings

logger = logging.getLogger(__name__)

PK = "GOV"
USAGE_PK = "USAGE"

DEFAULT_POLICY = {
    "daily_limit_per_user": 200,
    "daily_limit_total": 1000,
    "max_turns_cap": 15,
    "sources_enabled": {
        "debug": True,
        "api": True,
        "schedule": True,
        "channel": True,
        "eval": True,
        "pipeline": True,
    },
}


class QuotaExceeded(Exception):
    pass


class SourceDisabled(Exception):
    pass


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class GovernanceService:
    def __init__(self) -> None:
        dynamodb = boto3.resource("dynamodb", region_name=settings.aws_region)
        self.table = dynamodb.Table(settings.dynamo_table)

    # ------------------------------------------------------------- policy

    def get_policy(self) -> dict:
        resp = self.table.get_item(Key={"PK": PK, "SK": "POLICY"})
        item = resp.get("Item") or {}
        policy = dict(DEFAULT_POLICY)
        for k in ("daily_limit_per_user", "daily_limit_total", "max_turns_cap"):
            if k in item:
                policy[k] = int(item[k])
        if isinstance(item.get("sources_enabled"), dict):
            policy["sources_enabled"] = {
                **DEFAULT_POLICY["sources_enabled"],
                **{k: bool(v) for k, v in item["sources_enabled"].items()},
            }
        return policy

    def update_policy(self, patch: dict) -> dict:
        policy = self.get_policy()
        for k in ("daily_limit_per_user", "daily_limit_total", "max_turns_cap"):
            if patch.get(k) is not None:
                policy[k] = max(0, int(patch[k]))
        if isinstance(patch.get("sources_enabled"), dict):
            policy["sources_enabled"] = {
                **policy["sources_enabled"],
                **{k: bool(v) for k, v in patch["sources_enabled"].items()},
            }
        self.table.put_item(Item={"PK": PK, "SK": "POLICY", **policy})
        return policy

    # -------------------------------------------------------------- usage

    def _increment(self, sk: str) -> int:
        resp = self.table.update_item(
            Key={"PK": USAGE_PK, "SK": sk},
            UpdateExpression="ADD #c :one",
            ExpressionAttributeNames={"#c": "count"},
            ExpressionAttributeValues={":one": 1},
            ReturnValues="UPDATED_NEW",
        )
        return int(resp["Attributes"]["count"])

    def usage_today(self, user: str | None = None) -> dict:
        day = _today()
        keys = [{"PK": USAGE_PK, "SK": day}]
        if user:
            keys.append({"PK": USAGE_PK, "SK": f"{day}#USER#{user}"})
        out = {"date": day, "total": 0, "user": 0}
        for key in keys:
            item = self.table.get_item(Key=key).get("Item")
            count = int(item["count"]) if item and "count" in item else 0
            if key["SK"] == day:
                out["total"] = count
            else:
                out["user"] = count
        return out

    def check_and_count(self, user: str, source: str, max_turns: int | None = None) -> int:
        """Enforce the policy for one invocation and count it.

        Returns the (possibly capped) max_turns to use. Raises
        ``SourceDisabled`` / ``QuotaExceeded`` when the policy blocks the call.
        The counters increment *before* the invocation runs — a failed model
        call still consumed platform capacity.
        """
        policy = self.get_policy()
        if not policy["sources_enabled"].get(source, True):
            raise SourceDisabled(f"invocations from source '{source}' are disabled by policy")

        day = _today()
        total = self._increment(day)
        per_user = self._increment(f"{day}#USER#{user}")
        if policy["daily_limit_total"] and total > policy["daily_limit_total"]:
            raise QuotaExceeded(
                f"platform daily invocation limit reached ({policy['daily_limit_total']}/day)"
            )
        if policy["daily_limit_per_user"] and per_user > policy["daily_limit_per_user"]:
            raise QuotaExceeded(
                f"per-user daily invocation limit reached ({policy['daily_limit_per_user']}/day)"
            )

        cap = policy["max_turns_cap"]
        if max_turns is None:
            return cap
        return min(int(max_turns), cap) if cap else int(max_turns)


governance_service = GovernanceService()
