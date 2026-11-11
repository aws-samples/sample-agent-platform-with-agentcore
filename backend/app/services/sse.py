"""Server-Sent Events response for the streaming invoke routes."""

import json
from typing import Iterable

from fastapi.responses import StreamingResponse


def sse_lines(events: Iterable[dict]) -> Iterable[bytes]:
    for event in events:
        yield f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n".encode("utf-8")


def sse_response(events: Iterable[dict]) -> StreamingResponse:
    """One ``data: {json}`` line per event. The headers keep intermediaries
    (ALB, CloudFront, nginx) from buffering the stream into one response."""
    return StreamingResponse(
        sse_lines(events),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )
