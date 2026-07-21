"""Read access to per-session S3 workspaces (artifacts browser)."""

import logging

import boto3

from app.config import settings

logger = logging.getLogger(__name__)

MAX_INLINE_BYTES = 256 * 1024


class WorkspaceService:
    def __init__(self) -> None:
        self.s3 = boto3.client("s3", region_name=settings.aws_region)

    def _prefix(self, runtime_session_id: str) -> str:
        return f"{settings.workspace_prefix}/{runtime_session_id}/"

    def list_files(self, runtime_session_id: str) -> list[dict]:
        if not settings.workspace_bucket:
            return []
        prefix = self._prefix(runtime_session_id)
        paginator = self.s3.get_paginator("list_objects_v2")
        files = []
        for page in paginator.paginate(
            Bucket=settings.workspace_bucket, Prefix=prefix, PaginationConfig={"MaxItems": 500}
        ):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                rel = key[len(prefix):]
                # Claude Code internal state is synced for resume, not for browsing
                if rel.startswith(".claude-home/"):
                    continue
                files.append(
                    {
                        "key": rel,
                        "size": obj["Size"],
                        "last_modified": obj["LastModified"].isoformat(),
                    }
                )
        return files

    def read_file(self, runtime_session_id: str, rel_key: str) -> dict:
        full_key = f"{self._prefix(runtime_session_id)}{rel_key}"
        resp = self.s3.get_object(Bucket=settings.workspace_bucket, Key=full_key)
        body = resp["Body"].read(MAX_INLINE_BYTES + 1)
        truncated = len(body) > MAX_INLINE_BYTES
        return {
            "key": rel_key,
            "content": body[:MAX_INLINE_BYTES].decode("utf-8", errors="replace"),
            "truncated": truncated,
        }


workspace_service = WorkspaceService()
