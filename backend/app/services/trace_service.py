"""Orchestration traces for multi-step pipeline runs.

A pipeline run emits one X-Ray trace: a root segment (the run) with a
subsegment per phase and one per agent invocation, carrying cost / turns /
latency / session annotations. With CloudWatch Transaction Search enabled
these land in the ``aws/spans`` log group and render as an expandable trace
tree in the CloudWatch Traces console; the portal deep-links to it by trace ID.

Segments are buffered in memory and sent once at the end of the run — a
multi-phase pipeline run lasts minutes, so nothing is gained by streaming, and one
``PutTraceSegments`` batch keeps this dependency-free (plain boto3, identical
behavior on EKS and Lambda — no OTEL SDK, no collector sidecar).
"""

import json
import logging
import secrets
import time

import boto3

from app.config import settings

logger = logging.getLogger(__name__)

# PutTraceSegments accepts at most 50 documents per call
_BATCH = 50


def _num(v):
    """X-Ray annotations accept only str/int/float/bool."""
    return v if isinstance(v, (str, int, float, bool)) else str(v)


class TraceBuilder:
    """Collects one trace's segments; thread-safe enough for fan-out
    (list.append under the GIL) since builders are run-scoped."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.trace_id = f"1-{int(time.time()):08x}-{secrets.token_hex(12)}"
        self.root_id = secrets.token_hex(8)
        self._start = time.time()
        self._docs: list[dict] = []

    @staticmethod
    def new_id() -> str:
        return secrets.token_hex(8)

    def add_span(
        self,
        name: str,
        start: float,
        end: float,
        *,
        parent_id: str | None = None,
        span_id: str | None = None,
        annotations: dict | None = None,
        error: bool = False,
    ) -> str:
        sid = span_id or self.new_id()
        doc = {
            "name": name[:200],
            "id": sid,
            "trace_id": self.trace_id,
            "parent_id": parent_id or self.root_id,
            "start_time": start,
            "end_time": end,
            "type": "subsegment",
        }
        if annotations:
            doc["annotations"] = {k: _num(v) for k, v in annotations.items() if v is not None}
        if error:
            doc["error"] = True
        self._docs.append(doc)
        return sid

    def finish(self, *, annotations: dict | None = None, error: bool = False) -> None:
        """Close the root segment and ship the whole trace. Never raises —
        tracing must not fail a run."""
        root = {
            "name": self.name,
            "id": self.root_id,
            "trace_id": self.trace_id,
            "start_time": self._start,
            "end_time": time.time(),
        }
        if annotations:
            root["annotations"] = {k: _num(v) for k, v in annotations.items() if v is not None}
        if error:
            root["error"] = True
        docs = [json.dumps(d) for d in [root, *self._docs]]
        try:
            xray = boto3.client("xray", region_name=settings.aws_region)
            for i in range(0, len(docs), _BATCH):
                xray.put_trace_segments(TraceSegmentDocuments=docs[i : i + _BATCH])
        except Exception:
            logger.exception("trace export failed (run unaffected): %s", self.trace_id)
