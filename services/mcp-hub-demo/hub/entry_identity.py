"""IAM entry: trusting an identity header set by a gateway in front of the hub.

A private Amazon API Gateway (or any gateway with the same contract) can
authenticate the *application* with its own scheme — IAM / SigV4 in the
reference deployment — and forward two headers the caller cannot set:

    x-caller-arn            the authenticated identity
    x-mcp-hub-entry-secret  a shared secret only the gateway knows

The hub trusts the identity header only on requests carrying the right secret
(constant-time compare), and only when the identity is a session of the
configured caller role: ``arn:aws:sts::<acct>:assumed-role/<caller_role>/<actor>``.
The session name IS the actor — the agent platform assumes the caller role as
``agent-<id>`` for a published agent and ``dev-workbench`` for its workbench —
so there is no key material to mint, distribute or rotate.

Configuration (``entry_identity`` in the hub config)::

    entry_identity:
      identity_header: x-caller-arn            # default
      secret_header: x-mcp-hub-entry-secret    # default
      secret_env: HUB_ENTRY_SECRET             # env var holding the secret (default)
      caller_role: agent-platform-mcp-hub-caller
      allowed_actors: []                       # optional allowlist; empty = any session of the role

No block, or an empty secret in the environment, disables the entry: the hub
then accepts Bearer and MCPHUB-HMAC-SHA256 only. The acting user still rides
in X-MCPHUB-SSO-TOKEN and is verified exactly like a Bearer token.
"""

from __future__ import annotations

import hmac
import re
from collections.abc import Mapping

ASSUMED_ROLE_ARN = re.compile(r"^arn:aws:sts::\d{12}:assumed-role/([^/]+)/([^/]+)$")


def load_entry_identity(cfg: Mapping | None, environ: Mapping, log=None) -> dict | None:
    """The resolved entry settings, or None when the entry is off."""
    if not cfg:
        return None
    env_name = str(cfg.get("secret_env") or "HUB_ENTRY_SECRET")
    secret = str(environ.get(env_name, "") or "")
    if not secret:
        if log is not None:
            # Names nothing from the configuration: the variable's name is operator
            # input and the log line must stay free of anything secret-adjacent.
            log.warning("entry_identity is configured but the environment variable named by secret_env is empty; IAM entry disabled")
        return None
    role = str(cfg.get("caller_role") or "")
    if not role:
        raise SystemExit("entry_identity.caller_role is required")
    return {
        "identity_header": str(cfg.get("identity_header") or "x-caller-arn").lower(),
        "secret_header": str(cfg.get("secret_header") or "x-mcp-hub-entry-secret").lower(),
        "secret": secret,
        "caller_role": role,
        "allowed_actors": {str(a) for a in (cfg.get("allowed_actors") or [])},
    }


def entry_actor(headers: Mapping[str, str], entry: dict) -> tuple[str | None, str | None]:
    """(reason, actor) for a request that presents the entry secret header.

    ``reason`` is set when the request must be refused; ``actor`` is the
    verified session name otherwise. Header names are expected lower-cased.
    """
    presented = str(headers.get(entry["secret_header"], "") or "")
    if not presented or not hmac.compare_digest(presented.encode(), entry["secret"].encode()):
        return "entry secret mismatch", None
    match = ASSUMED_ROLE_ARN.match(str(headers.get(entry["identity_header"], "") or ""))
    if not match:
        return "caller is not an assumed-role session", None
    role, session = match.groups()
    if role != entry["caller_role"]:
        return f"caller role {role!r} is not the hub caller role", None
    if entry["allowed_actors"] and session not in entry["allowed_actors"]:
        return f"actor {session!r} is not registered", None
    return None, session
