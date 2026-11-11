"""lark-memory Lambda: ambient memory of the lark-teammate sample.

Two schedules invoke it: {"job": "consolidate"} hourly and {"job": "garden"}
weekly ({"dry_run": true} computes the gardener's deletions without applying
them). The logic is app/integrations/lark/ambient.py. It writes to the
sample's own memory store (PLATFORM_SAMPLE_MEMORY_IDS["lark-teammate"]) and
calls one small model directly (LARK_AMBIENT_MODEL), like the original.

Build & deploy: scripts/deploy-lark-webhook.sh (same package, both functions).
"""

import logging
import os

import boto3

from app.config import settings
from app.integrations.lark import ambient, ingress

logging.getLogger().setLevel(logging.INFO)
logger = logging.getLogger(__name__)

# an empty value (the variable set but blank) means the default
MODEL = os.environ.get("LARK_AMBIENT_MODEL") or "global.anthropic.claude-haiku-4-5-20251001-v1:0"


def _converse(system: str, text: str) -> str:
    resp = boto3.client("bedrock-runtime", region_name=settings.aws_region).converse(
        modelId=MODEL,
        system=[{"text": system}],
        messages=[{"role": "user", "content": [{"text": text}]}],
        inferenceConfig={"maxTokens": 1024, "temperature": 0.0},
    )
    return resp["output"]["message"]["content"][0]["text"]


def handler(event, context):
    event = event or {}
    memory_id = settings.sample_memory_ids.get(ingress.SAMPLE_ID, "")
    if not memory_id:
        raise RuntimeError(f"no memory store for sample {ingress.SAMPLE_ID} (PLATFORM_SAMPLE_MEMORY_IDS)")
    table = boto3.resource("dynamodb", region_name=settings.aws_region).Table(settings.dynamo_table)
    memory = boto3.client("bedrock-agentcore", region_name=settings.aws_region)
    job = event.get("job")
    if job == "consolidate":
        out = ambient.consolidate(client=ingress.client_from(ingress.lark_secret()), memory=memory,
                                  memory_id=memory_id, table=table, converse=_converse)
    elif job == "garden":
        out = ambient.garden(memory=memory, memory_id=memory_id, table=table, converse=_converse,
                             dry_run=bool(event.get("dry_run")))
    else:
        raise ValueError(f"unknown job {job!r}: expected consolidate or garden")
    logger.info("%s: groups=%s facts=%s deleted=%s", out["job"], out["groups"], out.get("facts"), out.get("deleted"))
    return {k: v for k, v in out.items() if k != "results"}
