# Bring your own MCP hub (replacing AgentCore Gateway)

AgentCore Gateway is the platform's default front door to existing APIs. Some
organizations want that layer under their own control instead — to avoid
coupling the tool-access path to a managed service, and to keep the routing
and authorization logic in code they can change. This document describes the
platform's support for a **customer-owned MCP hub** as the tool backend, and
the production-shaped pieces that come with it: a dedicated data plane for
published agents, and an application-authentication scheme
(`MCPHUB-HMAC-SHA256`).

AgentCore Gateway support is unchanged and remains available — the two
coexist per attachment (`kind: agentcore-gateway` vs `kind: mcp-hub`), and a
deployment that uses neither simply configures neither. Nothing in the
Terraform stack creates gateways; they were always deployed by the optional
scripts.

## The shape of the chain

<!-- ?v= bumps the URL so GitHub's image cache serves the current diagram -->
![MCP hub chains — production and development paths into a customer-owned hub](images/mcp-hub-chains.svg?v=2)

```
calling app ──SigV4 + robot token──► private service-entry API ──► entry pods (EKS)
    ──► published agent (AgentCore Runtime, VPC mode)
    ──MCPHUB-HMAC-SHA256 (per-agent Actor) + forwarded SSO token──► MCP hub
    ──Bearer (same token)──► backend MCP servers
```

One user identity travels the whole chain: the calling application's IdP
service account token. It enters as the `x-robot-token` header on the service
entry (verified against the platform's OIDC issuer), rides the existing
`{{user_token}}` identity-forwarding mechanism into the kernel, and reaches
the hub as `X-MCPHUB-SSO-TOKEN` — where the hub (and each backend,
independently) verifies it and derives the caller's permissions from its
claims. The HMAC signature answers a different question: *which application*
sent the request. On this platform that application is the **published
agent** — or, for pre-publish development in the workbench and Debug
console, the shared **dev-workbench** Actor (see "The development path"
below).

## The data-plane split

![Management / data-plane split — one image, two deployments](images/data-plane-split.svg?v=1)

Production agent traffic and the management console used to share one
backend deployment. They no longer do:

- **Management deployment** (`agent-platform-backend`) — portal APIs, Dev
  Workbench, publish, governance. Behind CloudFront with user auth.
- **Data-plane deployment** (`agent-platform-entry`) — the same image with
  `PLATFORM_ENTRY_ONLY=1`: it mounts *only* the IAM service entry
  (submit/poll for published agents) plus `/health`. The private
  service-entry API's NLB targets this service.

Consequences worth having: a vulnerability or misconfiguration in a console
route never fronts production traffic; the management service can be scaled
down, IP-restricted, or stopped outright without touching serving; and the
two scale independently (`backend_desired_count` vs `entry_desired_count`).
The management deployment still mounts the service-entry routes for
compatibility, but the API Gateway no longer sends traffic there.

## The application auth scheme

`MCPHUB-HMAC-SHA256` signs, per request: method, normalized path, canonical
query, a fixed five-header block (content type, path, nonce, timestamp, and
the **sha256 of the SSO token**), and the sha256 of the raw body. Signature =
HMAC-SHA256 over `ALGORITHM \n timestamp \n sha256(canonical request)` with
the Actor's secret key. Because the token hash and body hash are inside the
signature, neither can be swapped under an existing signature; because the
timestamp is signed, a capture is replayable for at most the clock-skew
window (±300 s by default, same as SigV4).

Static headers can't express this — the signature changes with every body —
so the kernel attaches an `mcp-hub` server through a **local signing proxy**
(`runtimes/agent-sdk-kernel/src/mcp_hub_proxy.py`): the same stdio shape as
`mcp-proxy-for-aws` (stdio → SigV4), with the customer's scheme in place of
SigV4. Message bytes are forwarded verbatim, so the body hash the hub
verifies is computed over exactly what the agent's CLI produced.

### Per-agent Actor credentials

Publishing an agent with an `mcp-hub` attachment mints that agent an
access/secret key pair in Secrets Manager
(`agent-platform/mcp-hub/{agent_id}`, access key `agent-{agent_id}`).
Republishing keeps the pair; deleting the agent retires it (7-day recovery).

- The **access key** is an identifier: it appears in the publish response,
  the portal card, and the hub's logs. Register it with your hub.
- The **secret key** never leaves Secrets Manager through the platform:
  invocation payloads carry only the secret *name*, and the signing proxy
  fetches the pair under the runtime role, which is granted exactly that
  prefix. Registering the pair with the hub is the hub operator's step — in
  the demo, a helper on the hub host is handed the secret *name* over SSM
  and pulls the value itself, so key material never transits the command
  log.
- **Rotation** is a two-step dance by design: rotate the secret
  (`McpHubCredentialsService.rotate` keeps the access key), then refresh the
  hub's actor registry before the next invocation.

### The development path: workbench and Debug console

Building an agent *against* the hub happens before publishing, so the Dev
Workbench (cloud-hosted Claude Code) and the Debug console can attach an
`mcp-hub` server too. Outside a published agent there is no per-agent pair;
those calls sign as one shared platform Actor instead:

- **Actor**: `dev-workbench` — a single access/secret pair, lazily minted
  into Secrets Manager (`…/mcp-hub/dev-workbench`) on the first attachment
  and registered with the hub once (`hmac actor verified: dev-workbench` in
  hub logs). The hub can rate-limit or revoke all development traffic as one
  application without touching any published agent.
- **User**: the *portal user's own* SSO token, forwarded per session/run.
  Hub-side permissions therefore stay per-user — two developers attaching
  the same hub see different tools if their departments differ. For this to
  verify, the portal client's tokens must carry the hub's audience and a
  `department` claim (the demo seed adds an audience mapper and a per-user
  attribute mapper; your IdP equivalent applies).
- **Token at rest**: the interactive kernel keeps the forwarded token in a
  file under `/tmp` inside the microVM — never in `/workspace/.mcp.json`,
  which syncs to S3. The signing proxy re-reads that file per request, so
  reconnecting a workbench session refreshes an expired token in place.

One asymmetry to know: a *portal user's* token has the IdP's normal (short)
lifetime, not the application service account's 8 hours. A workbench session
that outlives it will see hub calls fail with a 401 until the session is
reconnected.

### Where the SSO token comes from, and its lifetime  ⚠️

Application calls use the application's **service account** (client
credentials) token. The demo seeds one with an 8-hour access-token lifespan
— matching the AgentCore async ceiling, so a long run doesn't lose its tools
midway (every hop re-validates the token on every call, and there is no
refresh channel into a running agent).

**This is a development setting.** An 8-hour bearer token is 8 hours of
replayable credential if it leaks anywhere along the chain. Before
production, decide deliberately: shorter lifespans with runs bounded to
match, a token broker the hub trusts, or hub-side acceptance of expiry
mid-run for already-started requests. Write the decision down; don't ship
the demo default silently.

Scheduled and pipeline invocations have no calling application and therefore
no token today — they fail fast with `IdentityRequired`. Giving the platform
itself a service account for those sources is a straightforward extension,
deliberately not wired in this sample.

## Hub-side verification

The companion hub sample (`sample-mcp-hub-sso-auth`) accepts both inbound
forms against one identity model:

- `Bearer <jwt>` — a human ran the SSO flow themselves (unchanged).
- `MCPHUB-HMAC-SHA256 …` — an application; the hub rebuilds the canonical
  request from the received bytes, checks the signature against its actor
  registry (`actors:` in config, or the `HUB_ACTORS` env JSON), then
  verifies the SSO token exactly as it verifies a Bearer token. Department
  routing follows the token's claims either way — HMAC only adds "and we
  know which app sent it".

Production hardening the sample deliberately leaves out: **nonce
de-duplication** (within the ±300 s window a captured request is replayable;
add a nonce cache keyed by actor if your threat model cares), and **TLS on
the hub listener** (the demo runs HTTP inside private subnets; terminate TLS
in front of the hub before carrying real data).

## The IAM entry: no key pair at all

The HMAC scheme above authenticates the application with a key pair the
platform mints per agent and the hub operator registers. The platform also
offers a second path in which the application's identity **is its IAM role**
and no key material exists anywhere:

```
runtime ──SigV4 (execute-api; session agent-<id> of agent-platform-mcp-hub-caller)──►
  private REST API Gateway (AWS_IAM, this VPC's execute-api endpoint only)
  ──VPC Link──► internal NLB ──► MCP hub :8000
      + x-caller-arn (set by API Gateway from the authenticated identity)
      + x-mcp-hub-entry-secret (a shared secret only the gateway knows)
      + X-MCPHUB-SSO-TOKEN (the acting user, exactly as on the HMAC path)
```

![The MCP hub IAM entry: one tool call, hop by hop](images/mcp-hub-iam-entry.svg?v=1)

The pieces, and where each lives:

- **The entry** (`agent-platform-ops`, `modules/mcp_hub_demo/entry.tf`): a
  PRIVATE REST API whose three `/mcp` methods (POST, GET, DELETE) are
  `AWS_IAM`, reachable only through the platform VPC's execute-api endpoint
  (resource policy `aws:SourceVpce` + association), integrated over a VPC Link
  to an internal NLB in front of the hub. The integrations **stream**
  (`response_transfer_mode = STREAM`, MCP responses may be SSE) with the
  15-minute streaming timeout. API Gateway stamps `x-caller-arn` from the
  authenticated identity and a shared entry secret; the caller cannot set
  either. The same shape as the platform's own service entry.
- **The caller role** (`agent-platform-mcp-hub-caller`): one role whose
  **session name is the actor** — the hub reads it out of
  `arn:aws:sts::<acct>:assumed-role/agent-platform-mcp-hub-caller/agent-<id>`.
  It trusts only the **backend**: the backend pod's IRSA web-identity token
  directly (a first hop, so an 8-hour grant is possible; a session minted
  from another role session would be capped at one hour) and the backend role
  for runs outside a pod, both only for session names shaped `agent-*` or
  `dev-workbench`. The kernel roles cannot assume it at all. The role may do
  nothing but `execute-api:Invoke` on this one API. Same trust shape as the
  platform's inference caller role for the AgentCore Gateway model backend.
- **The backend** (`mcp_hub_credentials_service.mint_iam_session`): when it
  resolves an `iam` attachment for an invocation (or a workbench warmup) it
  mints an 8-hour caller-role session named after the actor it is serving,
  narrowed by a session policy to the one entry, and hands the credentials to
  the kernel in the warmup payload — the way the model-gateway grant
  (`llm_credentials`) already travels. The backend is the one component that
  knows which agent an invocation belongs to, so the session name is chosen
  there and nowhere else; a kernel cannot present another agent's identity.
  A re-warmup (workbench reconnect) mints a fresh session.
- **The kernel proxy** (`mcp_hub_proxy.py`, `MCPHUB_AUTH=iam`): SigV4-signs
  every POST (service `execute-api`, body and SSO token inside the signature)
  with the session it was handed — env JSON in the headless kernel, a 0600
  file under `/tmp` in the interactive kernel (never `.mcp.json`, which syncs
  to S3; re-read per request so a rotation lands in place). It holds no STS
  code and never sends `x-caller-arn` itself — that is the gateway's to set.
- **The hub** (`sample-mcp-hub-sso-auth`, `hub/entry_identity.py`): a third
  inbound branch next to Bearer and HMAC. On a request carrying the entry
  secret header (constant-time compare against `HUB_ENTRY_SECRET`, pulled
  by the hub host under its own role at boot), it trusts `x-caller-arn`,
  requires it to be a session of the configured `caller_role`, and takes the
  actor from the session name. Then the SSO token is verified exactly as on
  the other paths. A missing or wrong secret, a non-assumed-role ARN, another
  role, or (with `allowed_actors` set) an unregistered session is refused.
- **The registry entry**: `kind: mcp-hub` gains `auth: hmac | iam`. With
  `iam`, the target is the entry's invoke URL (facts `mcp_hub.entry_url`), the
  backend mints no key pair at publish (the agent card shows the actor
  `agent-<id>` instead of an access key), and the kernel payload carries
  `auth`, `actor` and `caller_role_arn` rather than a credentials secret. A
  deployment without a caller role (`PLATFORM_MCP_HUB_CALLER_ROLE_ARN`
  empty, i.e. no hub demo in that environment) refuses `iam` entries at
  registration and at publish rather than falling back to HMAC.

What changes in the trust model: on the HMAC path the runtime role can read
every agent's key pair, so any kernel could act as any agent. On the IAM path
a kernel holds exactly one session — the one the backend minted for the agent
it is running — and has no way to obtain another: per-agent identity becomes
a boundary the kernel cannot cross, with nothing left to leak or rotate (a
leaked session names its own actor and dies within eight hours). What is
lost: the HMAC signature covered the body and the token hash end to end; on
the IAM path the gateway verifies the SigV4 signature (which also covers body
and token) and the hub trusts the gateway's headers, so the hub's listener
must be reachable only through the gateway's path (the hub security group
admits the entry NLB; the runtime SG rule stays only while HMAC entries
remain). Keep the demo's HTTP-inside-the-VPC caveat in mind for both.

Both paths coexist per registry entry; `seed_mcp_hub_demo.py --auth iam|hmac`
registers either, and `e2e_mcp_hub.py` asserts from the hub's log which path
actually served the run (`HUB-AUTH-PATH`).

## Deploying the demo

```bash
# 1. package the hub source (any checkout shaped like sample-mcp-hub-sso-auth)
scripts/package_mcp_hub.sh ../sample-mcp-hub-sso-auth

# 2. hub EC2 + demo-app EC2 (requires enable_team_auth — the hub verifies
#    tokens against the platform's Keycloak)
terraform -chdir=terraform apply -var enable_mcp_hub_demo=true

# 3. wire everything: IdP client, registry entry (through the IAM entry by
#    default; --auth hmac for the key-pair path), demo agent, iam channel +
#    allowlist, hub actor sync (hmac only)
python3 scripts/seed_mcp_hub_demo.py

# 4. drive the whole chain from the calling application's seat
python3 scripts/e2e_mcp_hub.py
```

Network posture: the hub admits only the AgentCore runtime security group on
its MCP port; the demo-app instance has **no ingress at all** (operate it
over SSM); the service entry is a PRIVATE API reached through the VPC
endpoint. Nothing in this demo opens `0.0.0.0/0` inbound anywhere.

The hub announces a *logical* resource URL
(`http://mcp-hub.agent-platform.internal/mcp` by default) as its token
audience, deliberately decoupled from the instance's DNS name — replacing
the instance never invalidates issued tokens or the Keycloak audience
mapper. The registry entry's `target` is what points at the real endpoint;
`seed_mcp_hub_demo.py` re-registers it when the instance changes.

## Pointing at your own hub instead

The demo hub is a stand-in for whatever you run. The contract your hub must
satisfy:

1. **Transport**: MCP streamable HTTP, stateless JSON responses (one POST in,
   one JSON-RPC response out; 202 for notifications). SSE responses are
   tolerated by the proxy but not required.
2. **Auth**, one of:
   - verify `MCPHUB-HMAC-SHA256` exactly as `hub/mcphub_hmac.py` does in the
     hub sample (the same file signs on the kernel side, so the two cannot
     drift), or
   - sit behind the platform's IAM entry and trust `x-caller-arn` on requests
     carrying the entry secret, taking the actor from the assumed-role
     session name (`hub/entry_identity.py` in the sample);
   then validate `X-MCPHUB-SSO-TOKEN` against your IdP either way.
3. **Actor registry** (HMAC only): accept the per-agent access keys the
   platform mints and look up their secrets from wherever you keep them. On
   the IAM path the caller role's trust policy is the registry.

Then: register an `mcp-hub` entry whose target is your hub's URL (reachable
from the runtime VPC), attach it to an agent, publish, and hand your hub the
agent's credentials.
