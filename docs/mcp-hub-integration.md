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

```
calling app ──SigV4 + robot token──► private service-entry API ──► entry ECS
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

## Deploying the demo

```bash
# 1. package the hub source (any checkout shaped like sample-mcp-hub-sso-auth)
scripts/package_mcp_hub.sh ../sample-mcp-hub-sso-auth

# 2. hub EC2 + demo-app EC2 (requires enable_team_auth — the hub verifies
#    tokens against the platform's Keycloak)
terraform -chdir=terraform apply -var enable_mcp_hub_demo=true

# 3. wire everything: IdP client, registry entry, demo agent (mints its
#    Actor pair), iam channel + allowlist, hub actor sync
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
2. **Auth**: verify `MCPHUB-HMAC-SHA256` exactly as
   `hub/mcphub_hmac.py` does in the hub sample (the same file signs on the
   kernel side, so the two cannot drift), then validate `X-MCPHUB-SSO-TOKEN`
   against your IdP.
3. **Actor registry**: accept the per-agent access keys the platform mints
   and look up their secrets from wherever you keep them.

Then: register an `mcp-hub` entry whose target is your hub's URL (reachable
from the runtime VPC), attach it to an agent, publish, and hand your hub the
agent's credentials.
