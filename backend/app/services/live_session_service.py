"""Live-session ledger: every AgentCore Runtime session the platform opens.

AgentCore Runtime has no API that lists sessions or reports one's status
(boto3 >= 1.43.117 still ships none; ``ListSessions`` belongs to Memory). A
``runtimeSessionId`` stays valid until the runtime itself is deleted, and an
invocation of an id whose microVM was stopped or idle-reclaimed silently
provisions a fresh, empty microVM. So the only way to know which sessions the
platform has open is to write it down, and the only safe way to "check on" a
session is to read that record, never to invoke the id.

One row per runtime session id (``PK=LIVE_SESSION``, ``SK=<runtime session
id>``). Three kinds share the table:

``workbench``  interactive Dev Workbench sessions (written at connect / stop)
``headless``   synchronous and async SDK-kernel invocations
``sandbox``    one-shot code sandboxes (app/services/sandbox_service.py)

Workbench and headless rows are bookkeeping: a failed write is logged and
never fails the call it describes. Sandbox rows are the opposite: the sandbox
service writes the row *before* the first invocation and refuses to boot when
the write fails, because a sandbox whose id was never recorded can be neither
found nor stopped (see ``create``).

States (``status``):

  creating -> running -> idle -> stopping -> stopped
                      \\-> lost (the platform believes the microVM is gone)
                      \\-> stop_failed (a stop was attempted and errored)

A row in a terminal state carries a DynamoDB ``ttl`` (``retention_live_session_days``).
"""

import logging
import time
from datetime import datetime, timezone

import boto3
from botocore.config import Config
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

from app.config import settings
from app.services.retention import ttl_after_days, with_ttl

logger = logging.getLogger(__name__)

PK = "LIVE_SESSION"
KINDS = ("workbench", "headless", "sandbox")
OPEN_STATES = ("creating", "running", "idle", "stopping", "stop_failed")
TERMINAL_STATES = ("stopped", "lost")
STATES = OPEN_STATES + TERMINAL_STATES
# Sparse index of the open rows (the foundation's platform table declares it:
# agent-platform-ops terraform/modules/platform/main.tf). A row created through
# create() carries OPEN_ATTR = its kind until set_status() moves it to a
# terminal state, which REMOVEs the attribute and so drops the row from the
# index; the sandbox reaper reads the index instead of the partition and its
# week of stopped rows. touch() rows (workbench / headless bookkeeping) never
# carry it.
OPEN_INDEX = "open-sandboxes"
OPEN_ATTR = "open_kind"
# AgentCore's default idle timeout; a row idle for longer than this is
# presumed reclaimed (the runtimes the platform creates keep the default).
DEFAULT_IDLE_TIMEOUT_S = 900


class LedgerWriteFailed(RuntimeError):
    """The ledger row could not be written (sandbox create must not proceed)."""


class AlreadyExists(LedgerWriteFailed):
    """A row for this runtime session id already exists."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class LiveSessionService:
    def __init__(self) -> None:
        # the sandbox service writes and reads a row around every operation:
        # the same pool size as its agentcore client (config.py)
        dynamodb = boto3.resource(
            "dynamodb", region_name=settings.aws_region,
            config=Config(max_pool_connections=settings.aws_max_pool_connections),
        )
        self.table = dynamodb.Table(settings.dynamo_table)

    # ------------------------------------------------------------ writes

    def create(
        self,
        runtime_session_id: str,
        *,
        kind: str,
        owner: str,
        target: str,
        source: str = "",
        status: str = "creating",
        idle_timeout_s: int = DEFAULT_IDLE_TIMEOUT_S,
        max_lifetime_s: int = 0,
        attrs: dict | None = None,
    ) -> dict:
        """Write a new row; raise ``AlreadyExists`` / ``LedgerWriteFailed``.

        Conditional on the id not existing: a sandbox id is minted fresh for
        every run, so a collision means a bug (or a replay) and must stop the
        boot rather than overwrite another run's record."""
        if kind not in KINDS:
            raise ValueError(f"unknown live-session kind {kind!r}")
        if status not in STATES:
            raise ValueError(f"unknown live-session status {status!r}")
        now = time.time()
        item = {
            "PK": PK,
            "SK": runtime_session_id,
            "runtime_session_id": runtime_session_id,
            "kind": kind,
            "owner": str(owner),
            "target": target,
            "source": source,
            "status": status,
            "created_at": _now_iso(),
            "created_ts": int(now),
            "updated_ts": int(now),
            "last_invoke_ts": int(now),
            "idle_timeout_s": int(idle_timeout_s),
            "max_lifetime_s": int(max_lifetime_s),
            **(attrs or {}),
        }
        if status not in TERMINAL_STATES:
            item[OPEN_ATTR] = kind
        try:
            self.table.put_item(
                Item=item, ConditionExpression="attribute_not_exists(SK)"
            )
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                raise AlreadyExists(runtime_session_id) from e
            raise LedgerWriteFailed(str(e)) from e
        except Exception as e:  # noqa: BLE001 - surfaced to the caller as one type
            raise LedgerWriteFailed(str(e)) from e
        return item

    def touch(
        self,
        runtime_session_id: str,
        *,
        kind: str,
        owner: str,
        target: str,
        source: str = "",
        status: str = "running",
        idle_timeout_s: int = DEFAULT_IDLE_TIMEOUT_S,
        attrs: dict | None = None,
    ) -> None:
        """Upsert for bookkeeping rows (workbench / headless). Never raises.

        A reused id (Debug console continuity, a reconnecting workbench) moves
        back to ``status`` and gets its idle clock reset; ``created_*`` stays.
        Any TTL a previous stop set is removed, because the session is live
        again."""
        now = int(time.time())
        names = {"#s": "status", "#k": "kind", "#o": "owner", "#t": "target", "#src": "source", "#ttl": "ttl"}
        values = {
            ":s": status,
            ":k": kind,
            ":o": str(owner),
            ":t": target,
            ":src": source,
            ":now": now,
            ":iso": _now_iso(),
            ":idle": int(idle_timeout_s),
        }
        sets = [
            "#s = :s", "#k = :k", "#o = :o", "#t = :t", "#src = :src",
            "updated_ts = :now", "last_invoke_ts = :now", "idle_timeout_s = :idle",
            "runtime_session_id = if_not_exists(runtime_session_id, :rsid)",
            "created_at = if_not_exists(created_at, :iso)",
            "created_ts = if_not_exists(created_ts, :now)",
        ]
        values[":rsid"] = runtime_session_id
        for i, (k, v) in enumerate((attrs or {}).items()):
            names[f"#a{i}"] = k
            values[f":a{i}"] = v
            sets.append(f"#a{i} = :a{i}")
        try:
            self.table.update_item(
                Key={"PK": PK, "SK": runtime_session_id},
                UpdateExpression="SET " + ", ".join(sets) + " REMOVE #ttl",
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
            )
        except Exception:  # noqa: BLE001 - bookkeeping must not fail the call
            logger.exception("live-session touch failed (%s)", runtime_session_id)

    def set_status(
        self,
        runtime_session_id: str,
        status: str,
        *,
        expect: tuple[str, ...] | None = None,
        attrs: dict | None = None,
        strict: bool = False,
    ) -> bool:
        """Move a row to ``status``; returns False when ``expect`` did not hold.

        ``expect`` makes the transition conditional on the current status, so
        two actors (the API stopping a sandbox, the reaper reclaiming it) cannot
        both win. ``strict`` raises ``LedgerWriteFailed`` on any other error
        instead of logging it (the sandbox path wants to know)."""
        if status not in STATES:
            raise ValueError(f"unknown live-session status {status!r}")
        now = int(time.time())
        names = {"#s": "status"}
        values: dict = {":s": status, ":now": now}
        sets = ["#s = :s", "updated_ts = :now"]
        removes: list[str] = []
        if status in TERMINAL_STATES:
            # out of the open index in the same write that closes the row
            names["#open"] = OPEN_ATTR
            removes.append("#open")
            ttl = ttl_after_days(settings.retention_live_session_days)
            if ttl is not None:
                names["#ttl"] = "ttl"
                values[":ttl"] = ttl
                sets.append("#ttl = :ttl")
            values[":ended"] = _now_iso()
            sets.append("ended_at = if_not_exists(ended_at, :ended)")
        for i, (k, v) in enumerate((attrs or {}).items()):
            names[f"#a{i}"] = k
            values[f":a{i}"] = v
            sets.append(f"#a{i} = :a{i}")
        kwargs: dict = {
            "Key": {"PK": PK, "SK": runtime_session_id},
            "UpdateExpression": "SET " + ", ".join(sets) + (" REMOVE " + ", ".join(removes) if removes else ""),
            "ExpressionAttributeNames": names,
            "ExpressionAttributeValues": values,
        }
        cond = "attribute_exists(SK)"
        if expect:
            placeholders = []
            for i, st in enumerate(expect):
                values[f":e{i}"] = st
                placeholders.append(f":e{i}")
            cond += f" AND #s IN ({', '.join(placeholders)})"
        kwargs["ConditionExpression"] = cond
        try:
            self.table.update_item(**kwargs)
            return True
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return False
            if strict:
                raise LedgerWriteFailed(str(e)) from e
            logger.exception("live-session status update failed (%s)", runtime_session_id)
            return False
        except Exception as e:  # noqa: BLE001
            if strict:
                raise LedgerWriteFailed(str(e)) from e
            logger.exception("live-session status update failed (%s)", runtime_session_id)
            return False

    def heartbeat(self, runtime_session_id: str) -> None:
        """Record that the platform just invoked this session (resets the
        presumed-idle clock). Never raises."""
        try:
            self.table.update_item(
                Key={"PK": PK, "SK": runtime_session_id},
                UpdateExpression="SET last_invoke_ts = :now, updated_ts = :now",
                ConditionExpression="attribute_exists(SK)",
                ExpressionAttributeValues={":now": int(time.time())},
            )
        except Exception:  # noqa: BLE001
            logger.debug("live-session heartbeat skipped (%s)", runtime_session_id)

    # ------------------------------------------------------------- reads

    def get(self, runtime_session_id: str) -> dict | None:
        resp = self.table.get_item(Key={"PK": PK, "SK": runtime_session_id}, ConsistentRead=True)
        item = resp.get("Item")
        return _public(item) if item else None

    def list_rows(
        self,
        *,
        kind: str = "",
        owner: str = "",
        open_only: bool = True,
        limit: int = 500,
    ) -> list[dict]:
        """Rows newest first. The table is small (open sessions plus a week of
        stopped ones), so a partition query with a filter is enough."""
        filt = None
        if open_only:
            filt = Attr("status").is_in(list(OPEN_STATES))
        if kind:
            f = Attr("kind").eq(kind)
            filt = f if filt is None else filt & f
        if owner:
            f = Attr("owner").eq(str(owner))
            filt = f if filt is None else filt & f
        kwargs: dict = {"KeyConditionExpression": Key("PK").eq(PK)}
        if filt is not None:
            kwargs["FilterExpression"] = filt
        items: list[dict] = []
        while True:
            resp = self.table.query(**kwargs)
            items.extend(resp.get("Items", []))
            last = resp.get("LastEvaluatedKey")
            if not last or len(items) >= limit:
                break
            kwargs["ExclusiveStartKey"] = last
        rows = [_public(i) for i in items[:limit]]
        return sorted(rows, key=lambda r: r.get("created_ts", 0), reverse=True)

    def summary(self) -> dict:
        """Open rows counted per kind and status."""
        out: dict = {k: {} for k in KINDS}
        for r in self.list_rows(open_only=True, limit=10_000):
            bucket = out.setdefault(r["kind"], {})
            bucket[r["status"]] = bucket.get(r["status"], 0) + 1
        return out


def presumed_state(row: dict, now: float | None = None) -> str:
    """What the platform can say about a session without touching it.

    ``running`` / ``idle`` rows whose last invocation is older than the
    session's idle timeout are presumed reclaimed by AgentCore: the record
    cannot know for sure, and finding out would mean invoking the id, which
    boots a new microVM. Everything else is reported as recorded."""
    now = time.time() if now is None else now
    status = row.get("status", "")
    if status in ("running", "idle"):
        idle_for = now - int(row.get("last_invoke_ts") or 0)
        if idle_for > int(row.get("idle_timeout_s") or DEFAULT_IDLE_TIMEOUT_S):
            # a sandbox with a running background job keeps its microVM alive
            # through /ping HealthyBusy, so only the sandbox service's own
            # heartbeat (refreshed while it polls) proves it is still in use
            return "presumed_reclaimed"
    return status


# Fields a row may carry that never leave the backend. ``nonce`` is the
# sandbox claim secret (any replica must be able to sign with it); returning
# it would let whoever reads the API answer as the sandbox's host.
PRIVATE_FIELDS = frozenset({"PK", "SK", "ttl", "nonce", "nonce_sha256"})


def _public(item: dict) -> dict:
    out = {k: v for k, v in item.items() if k not in PRIVATE_FIELDS}
    for k in ("created_ts", "updated_ts", "last_invoke_ts", "idle_timeout_s", "max_lifetime_s"):
        if k in out and out[k] is not None:
            out[k] = int(out[k])
    out["presumed_state"] = presumed_state(out)
    return out


live_session_service = LiveSessionService()
