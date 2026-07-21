"""Platform audit log (Governance).

Every mutating platform action is appended as an immutable audit event under
``PK=AUDIT``. The sort key is ``{iso timestamp}#{id}`` so a single reverse
query returns the newest events first.
"""

import logging
import uuid
from datetime import datetime, timezone

import boto3

from app.config import settings

logger = logging.getLogger(__name__)

PK = "AUDIT"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class AuditService:
    def __init__(self) -> None:
        dynamodb = boto3.resource("dynamodb", region_name=settings.aws_region)
        self.table = dynamodb.Table(settings.dynamo_table)

    def record(self, user: str, action: str, resource: str = "", detail: str = "") -> None:
        """Append an audit event. Never raises — auditing must not break the
        action being audited."""
        try:
            ts = _now()
            self.table.put_item(
                Item={
                    "PK": PK,
                    "SK": f"{ts}#{uuid.uuid4().hex[:8]}",
                    "ts": ts,
                    "user": user,
                    "action": action,
                    "resource": resource[:300],
                    "detail": detail[:500],
                }
            )
        except Exception:
            logger.exception("audit record failed: %s %s", action, resource)

    def list_events(self, limit: int = 100) -> list[dict]:
        resp = self.table.query(
            KeyConditionExpression="PK = :pk",
            ExpressionAttributeValues={":pk": PK},
            ScanIndexForward=False,
            Limit=min(limit, 200),
        )
        return [
            {
                "ts": i.get("ts", ""),
                "user": i.get("user", ""),
                "action": i.get("action", ""),
                "resource": i.get("resource", ""),
                "detail": i.get("detail", ""),
            }
            for i in resp.get("Items", [])
        ]


audit_service = AuditService()
