"""IAM entry: trusting an identity header set by a gateway in front of the hub.

A private Amazon API Gateway (or any gateway with the same contract) can
authenticate the *application* with its own scheme - IAM / SigV4 in the
reference deployments - and forward the authenticated identity in a header
the caller cannot set:

    x-caller-arn            the authenticated identity (context.identity.userArn)

Two questions decide whether the hub may believe that header, and the
``entry_identity`` block answers each one explicitly.

1. *Did the request really come through the gateway?* (``trust``)

   ``secret`` (default)  the gateway also forwards a shared secret
                         (``x-mcp-hub-entry-secret``) that the hub compares in
                         constant time against ``HUB_ENTRY_SECRET``. An empty
                         secret disables the entry.
   ``network``           no shared secret. The hub's listener is reachable
                         *only* through the gateway (its VPC Link), enforced
                         outside the hub. That includes whatever sits in front
                         of the hub: traffic forwarded by an NLB arrives from
                         the NLB's addresses, so the NLB itself must admit the
                         VPC Link and nothing else (no inbound rule, PrivateLink
                         not evaluated). Use it when the deployment must hold
                         no static key at all; it is only as strong as that
                         network isolation.

2. *Which application is it?* (``caller_role`` and/or ``callers``)

   ``caller_role``       one role whose SESSION NAME is the actor:
                         ``arn:aws:sts::<acct>:assumed-role/<caller_role>/<actor>``.
                         The agent platform mints those sessions itself
                         (``agent-<id>`` / ``dev-workbench``).
   ``callers``           a list of ``{role_arn, actor}``: each role IS one
                         application, whatever its session is called. This is
                         the shape for workloads that authenticate with their
                         own role directly - an EKS pod under IRSA gets a
                         generated session name (``botocore-session-...``), so
                         the role, not the session, names the caller. Partition,
                         account and role name must all match.

   When both are set, ``callers`` is checked first.

``require_user_token`` (default true): the acting user's SSO token in
X-MCPHUB-SSO-TOKEN is mandatory, as on every other path. Set it to false for
application-only calls (pod-to-pod with IAM and no end user): the hub then
routes to no backend that needs a user, and exposes ``hub__whoami`` so the
caller can see the identity the hub resolved. A token that *is* presented is
still verified.

Example (pod-to-pod, no static key)::

    entry_identity:
      trust: network
      require_user_token: false
      callers:
        - role_arn: arn:aws:iam::<acct>:role/mcp-frontend
          actor: frontend-app

No block disables the entry: the hub then accepts Bearer and
MCPHUB-HMAC-SHA256 only.
"""

from __future__ import annotations

import hmac
import re
from collections.abc import Mapping

_NAME = r"[A-Za-z0-9+=,.@_-]+"
ASSUMED_ROLE_ARN = re.compile(rf"arn:(aws|aws-cn|aws-us-gov):sts::(\d{{12}}):assumed-role/({_NAME})/({_NAME})")
ROLE_ARN = re.compile(rf"arn:(aws|aws-cn|aws-us-gov):iam::(\d{{12}}):role/(?:[A-Za-z0-9+=,.@_/-]*/)?({_NAME})")
ACTOR = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
# All three are used with fullmatch: a "$" anchor would also accept a trailing
# newline, and an identity header must be exactly one ARN.
TRUST_MODES = ("secret", "network")


def _callers(cfg: Mapping) -> dict[tuple[str, str, str], str]:
    """{(partition, account, role name): actor} from the ``callers`` list.

    An assumed-role ARN carries the role's name but not its path, so a role
    is matched by partition + account + name; the path in ``role_arn`` is
    accepted and ignored.
    """
    out: dict[tuple[str, str, str], str] = {}
    for item in cfg.get("callers") or []:
        if not isinstance(item, Mapping):
            raise SystemExit("entry_identity.callers entries must be mappings with role_arn and actor")
        m = ROLE_ARN.fullmatch(str(item.get("role_arn") or ""))
        if not m:
            raise SystemExit("entry_identity.callers: role_arn must be an IAM role ARN (arn:aws:iam::<acct>:role/<name>)")
        actor = str(item.get("actor") or "")
        if not ACTOR.fullmatch(actor):
            raise SystemExit("entry_identity.callers: actor must be 1-64 characters of [A-Za-z0-9._-]")
        key = (m.group(1), m.group(2), m.group(3))
        if out.get(key, actor) != actor:
            raise SystemExit("entry_identity.callers: one role mapped to two actors")
        out[key] = actor
    return out


def load_entry_identity(cfg: Mapping | None, environ: Mapping, log=None) -> dict | None:
    """The resolved entry settings, or None when the entry is off."""
    if not cfg:
        return None
    trust = str(cfg.get("trust") or "secret")
    if trust not in TRUST_MODES:
        raise SystemExit(f"entry_identity.trust must be one of: {', '.join(TRUST_MODES)}")
    secret = ""
    if trust == "secret":
        env_name = str(cfg.get("secret_env") or "HUB_ENTRY_SECRET")
        secret = str(environ.get(env_name, "") or "")
        if not secret:
            if log is not None:
                # Names nothing from the configuration: the variable's name is operator
                # input and the log line must stay free of anything secret-adjacent.
                log.warning("entry_identity is configured but the environment variable named by secret_env is empty; IAM entry disabled")
            return None
    role = str(cfg.get("caller_role") or "")
    callers = _callers(cfg)
    if not role and not callers:
        raise SystemExit("entry_identity needs caller_role or callers")
    require_user_token = cfg.get("require_user_token", True)
    if not isinstance(require_user_token, bool):
        raise SystemExit("entry_identity.require_user_token must be true or false")
    return {
        "trust": trust,
        "identity_header": str(cfg.get("identity_header") or "x-caller-arn").lower(),
        "secret_header": str(cfg.get("secret_header") or "x-mcp-hub-entry-secret").lower(),
        "secret": secret,
        "caller_role": role,
        "callers": callers,
        "allowed_actors": {str(a) for a in (cfg.get("allowed_actors") or [])},
        "require_user_token": require_user_token,
    }


def is_entry_request(headers: Mapping[str, str], entry: dict) -> bool:
    """Whether the request must be judged as an entry request.

    secret mode: it presents the secret header. network mode: it carries the
    identity header (or a secret header) - and is then judged here, never
    allowed to fall through to another branch with a forged identity
    attached.
    """
    if entry["trust"] == "network":
        return entry["identity_header"] in headers or entry["secret_header"] in headers
    return entry["secret_header"] in headers


def entry_actor(headers: Mapping[str, str], entry: dict) -> tuple[str | None, str | None]:
    """(reason, actor) for a request that came through the entry.

    ``reason`` is set when the request must be refused; ``actor`` is the
    verified application otherwise. Header names are expected lower-cased.
    """
    if entry["trust"] == "secret":
        presented = str(headers.get(entry["secret_header"], "") or "")
        if not presented or not hmac.compare_digest(presented.encode(), entry["secret"].encode()):
            return "entry secret mismatch", None
    match = ASSUMED_ROLE_ARN.fullmatch(str(headers.get(entry["identity_header"], "") or ""))
    if not match:
        return "caller is not an assumed-role session", None
    partition, account, role, session = match.groups()
    actor = entry["callers"].get((partition, account, role))
    if actor is None:
        if not entry["caller_role"] or role != entry["caller_role"]:
            return f"caller role {role!r} is not a registered caller", None
        actor = session
    if entry["allowed_actors"] and actor not in entry["allowed_actors"]:
        return f"actor {actor!r} is not registered", None
    return None, actor
