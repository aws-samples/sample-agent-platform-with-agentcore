"""AgentCore gateway entry: a hub that sits behind an Amazon Bedrock AgentCore gateway.

The gateway reaches the hub over a private endpoint. By the time a request
arrives it has authenticated the caller (a user's JWT, or an application's
SigV4 signature) and authorized the tool call (Cedar). What the hub can still
know about a request, and what it cannot:

  the user         when the gateway exchanges the inbound user token and sends
                   the result as a Bearer. The hub verifies it as on any other
                   path.
  the application  only that it is "whatever the gateway admitted". A gateway
                   does not forward a caller's IAM identity to an MCP server
                   target, so a request without a Bearer is an application-only
                   call attributed to the configured actor.

``trust`` is ``network`` and nothing else: no secret travels with the request,
so the listener must be reachable only from the gateway's private endpoint,
enforced outside the hub. It cannot be combined with ``entry_identity``: a
listener has one answer to "who is in front of me".

    gateway_entry:
      trust: network
      actor: agentcore-gateway
      allowed_departments: [team-a]   # optional, see below

``allowed_departments`` is the hub's own say over the users the gateway lets
through: a call with a user token runs a tool only when the user's departments
meet this list. It is checked at call time, not in tools/list: the gateway
synchronizes the tool list with its own service identity, so hiding the tool
there would hide it from everyone. Absent means the hub adds no rule of its
own (the gateway's Cedar policy is the only one). It is never empty when set:
an empty list would refuse every user while looking like a rule. An
application-only call (no user) is not subject to it; which applications may
call is decided by the IAM gateway's policy.

This module holds the decisions and nothing else, so they can be tested
without the MCP SDK: whether the block is valid, what one request's headers
amount to, and whether a user may run a tool.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

ACTOR = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
# Headers that name an application on the hub's other paths (the IAM entry's
# identity and secret, the HMAC scheme's user token). Behind a gateway they
# prove nothing, so a request carrying one is refused rather than ignored.
REFUSED_HEADERS = ("x-caller-arn", "x-mcp-hub-entry-secret", "x-mcphub-sso-token")
BEARER = "Bearer "
DEPARTMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


def load_gateway_entry(cfg, has_entry_identity: bool) -> dict | None:
    """The resolved gateway entry, or None when the block is absent."""
    if not cfg:
        return None
    if not isinstance(cfg, Mapping) or cfg.get("trust") != "network":
        raise SystemExit("gateway_entry.trust must be 'network'")
    actor = cfg.get("actor")
    if not isinstance(actor, str) or not ACTOR.fullmatch(actor):
        raise SystemExit("gateway_entry.actor must be 1-64 characters of [A-Za-z0-9._-]")
    if has_entry_identity:
        raise SystemExit("gateway_entry and entry_identity cannot both be set on one listener")
    unknown = set(cfg) - {"trust", "actor", "allowed_departments"}
    if unknown:
        raise SystemExit(f"gateway_entry: unknown keys {sorted(map(str, unknown))}")
    entry = {"actor": actor, "trust": "network", "via": "agentcore-gateway"}
    if "allowed_departments" in cfg:
        allowed = cfg["allowed_departments"]
        if (not isinstance(allowed, list) or not allowed
                or any(not isinstance(d, str) or not DEPARTMENT.fullmatch(d) for d in allowed)):
            raise SystemExit("gateway_entry.allowed_departments must be a non-empty list of names of [A-Za-z0-9._-]")
        entry["allowed_departments"] = sorted(set(allowed))
    return entry


def gateway_request(headers: Mapping[str, str], entry: dict) -> tuple[str | None, str | None, dict | None]:
    """(reason, user token, application) for one request behind the gateway.

    ``reason`` is set when the request must be refused. Otherwise the token is
    the exchanged user token to verify, or "" for an application-only call,
    and the application is what the hub attributes the call to. Header names
    are expected lower-cased.

    A Bearer is a user; no Authorization at all is the application alone.
    Everything else - an empty or blank Bearer, another scheme, a header that
    names an application on another path - is refused, never downgraded to an
    application-only call: that would turn a malformed credential into access.
    """
    if any(name in headers for name in REFUSED_HEADERS):
        return "gateway entry: unexpected identity header", None, None
    # What the call is attributed to: the entry's identity, never its rules.
    application = {k: v for k, v in entry.items() if k != "allowed_departments"}
    application["caller_arn"] = None
    if "authorization" not in headers:
        return None, "", application
    auth = headers["authorization"]
    if auth.startswith(BEARER) and auth[len(BEARER):].strip():
        return None, auth[len(BEARER):], application
    return "gateway entry: unsupported credentials", None, None


def user_may_call(entry: dict, departments: set[str], has_user: bool) -> str | None:
    """None when the call may run a tool, else why the hub refuses it.

    Only calls that carry a user are judged; with no allowed_departments the
    hub adds no rule of its own.
    """
    allowed = entry.get("allowed_departments")
    if not has_user or allowed is None:
        return None
    if set(departments) & set(allowed):
        return None
    return f"hub policy: departments {sorted(departments)} may not call tools here (allowed: {allowed})"
