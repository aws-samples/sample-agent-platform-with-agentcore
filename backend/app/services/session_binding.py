"""Caller-binding for client-controllable AgentCore session identifiers.

An AgentCore ``runtimeSessionId`` is not a label: reusing one routes to the
*same warm microVM and the same kernel process*, so two callers that share a
runtime session id share ``/tmp`` working files, the process secret cache and
any in-process model-gateway grant. That makes the id an authorization
boundary in exactly the way the memory actor id is (see
``invocation_service.resolve_memory_actor``) — and it must be treated the same
way: a value the caller can influence must never resolve onto another tenant's
session.

Two entry points, mirroring the two ways a session id enters the pipeline:

``resolve_session_id`` — for the API boundary (Debug console, published-agent
invoke), where the caller submits ``session_id`` verbatim. The submitted value
is namespaced under the *authenticated* caller (an HMAC that folds in the
caller identity), so caller A's ``"foo"`` and caller B's ``"foo"`` can never
collide onto one microVM, and a raw id lifted from someone else's response or
ledger entry re-namespaces under whoever replays it instead of landing on the
original session. The mapping is idempotent: the resolved id is echoed back to
the client (``InvokeResponse.runtime_session_id``) and the Debug console
resends it as ``session_id`` for conversation continuity, so feeding a
resolved id back in must return it unchanged.

``derive_channel_session_id`` — for the channel webhook path, where the id is
built server-side from a ``conversation_id``. The digest is keyed with the
platform binding secret so it is not offline-computable from the public
``channel_id`` (which is embedded in the webhook URL) plus a guessed
``conversation_id``.

The binding secret is ``PLATFORM_SESSION_BINDING_SECRET`` when set. The
fallback folds in deployment identifiers that do not appear in any public
webhook URL (the runtime ARNs, the table name, the API token), so an external
caller who knows only the channel URL still cannot predict a session id.
Setting the secret explicitly is strongly recommended for production; the
value only has to be stable across replicas and restarts, not rotated.
"""

import hashlib
import hmac
import secrets

from app.config import settings

# runtimeSessionId charset is [A-Za-z0-9_-] and AgentCore requires >= 33 chars.
# Both forms below are 47/48 chars: prefix + 12-char caller tag + 32-char body.
_SID_PREFIX = "s"


def _secret() -> bytes:
    explicit = getattr(settings, "session_binding_secret", "") or ""
    if explicit:
        return explicit.encode("utf-8")
    seed = "|".join(
        str(x)
        for x in (
            settings.sdk_runtime_arn,
            settings.interactive_runtime_arn,
            settings.dynamo_table,
            settings.aws_region,
            settings.api_token,
        )
    )
    return ("session-binding:" + seed).encode("utf-8")


def _hmac(label: bytes, message: bytes) -> str:
    return hmac.new(_secret(), label + b"\x00" + message, hashlib.sha256).hexdigest()


def _caller_tag(user: str) -> str:
    """A stable, per-caller prefix segment. Identifies whose namespace a
    resolved id lives in without revealing the caller name."""
    return _hmac(b"utag", str(user).encode("utf-8"))[:12]


def resolve_session_id(user, requested: str | None) -> str:
    """Map a caller-submitted ``session_id`` onto a caller-bound runtime
    session id. Never returns another caller's id.

    - empty request  -> a fresh id already inside this caller's namespace
      (so the very first turn's echoed id round-trips cleanly);
    - a value this caller already owns -> returned unchanged (continuity);
    - anything else (a raw client id, or an id minted for another caller)
      -> namespaced under this caller, which can never collide onto another
      tenant's microVM.
    """
    tag = _caller_tag(str(user))
    if not requested:
        return f"{_SID_PREFIX}-{tag}-{secrets.token_hex(16)}"
    parts = requested.split("-", 2)
    if (
        len(parts) == 3
        and parts[0] == _SID_PREFIX
        and hmac.compare_digest(parts[1], tag)
    ):
        # one of this caller's own ids, echoed back for continuity
        return requested
    return f"{_SID_PREFIX}-{tag}-{_hmac(b'sid', str(user).encode('utf-8') + b'|' + requested.encode('utf-8'))[:32]}"


def derive_channel_session_id(channel_id: str, conversation_id: str) -> str:
    """Stable, non-guessable runtime session id for a channel conversation.

    Keyed with the binding secret so it cannot be reproduced offline from the
    public ``channel_id`` plus a guessed ``conversation_id``."""
    digest = _hmac(b"chn", f"{channel_id}\x00{conversation_id}".encode("utf-8"))
    return f"chn-{digest[:44]}"  # >= 33 chars for AgentCore
