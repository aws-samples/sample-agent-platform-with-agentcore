# SSO-Authenticated MCP Hub for Claude Code

> Part of the agent platform sample since 2026-10: this is the **companion
> customer-owned hub** that the platform's `mcp_hub_demo` Terraform module
> deploys and that [`docs/mcp-hub-integration.md`](../../docs/mcp-hub-integration.md)
> describes. It stands in for *your* hub, so everything below runs on its own
> (`docker compose up`, no AWS needed) and the platform-facing pieces are
> confined to two files: `hub/mcphub_hmac.py` (the HMAC key-pair scheme) and
> `hub/entry_identity.py` (the IAM entry). Licensed with the repository (MIT-0).

A runnable sample that replaces static per-user API keys with **enterprise SSO
login** for Claude Code's access to internal MCP servers — using nothing but
standard OAuth 2.1. One `docker compose up`, three demo users, and the same
Claude Code shows different tools and different data depending on who signed in.

![Architecture: humans with a Bearer JWT, applications through an HMAC key pair or the IAM entry](docs/images/architecture.svg?v=2)

## The problem this solves

A common first setup looks like this: every developer gets a long-lived access
token, pasted into their MCP config. Those tokens never expire, identify nobody
(or worse, identify a shared service account), and leak easily.

This sample shows the standards-based fix:

1. **Claude Code signs the user in through the corporate IdP** (browser SSO,
   PKCE). No secrets in config files — Claude Code discovers the IdP from the
   MCP server itself and stores/refreshes the token on its own.
2. **The token is a short-lived JWT** carrying the user's identity and
   department (any claim your IdP can emit). Lifetime is IdP policy — this
   sample issues 1-hour access tokens inside a 24-hour SSO session, so users
   re-authenticate once a day.
3. **An MCP Hub does coarse routing only**: verify the JWT, decide *which
   backend MCP servers this department may reach*, forward the call with the
   user's own token attached.
4. **Backends keep the real authority**: each one re-verifies the same JWT and
   decides *which rows and fields* the caller may see. The hub stays thin and
   generic; data-level policy lives with the data.

```
Claude Code ──SSO──▶ IdP (Keycloak)
     │                   ▲    ▲
     │ Bearer JWT        │    │  (both verify the same JWKS)
     ▼                   │    │
  MCP Hub ── routing ────┘    │
     │  department → backend  │
     ▼                        │
  MCP-1 (orders)   MCP-2 (HR)─┘   ← data-level authorization happens here
```

## Quick start

Requirements: Docker with compose, outbound internet (Keycloak fetches Claude
Code's client metadata from `claude.ai`), and Claude Code ≥ 2.1.

```bash
docker compose up -d --build     # Keycloak + hub + two backend MCP servers
                                 # first boot takes ~40s (realm import)

claude mcp add --transport http mcp-hub http://localhost:8000/mcp
claude mcp login mcp-hub         # browser opens → sign in as a demo user
```

Demo users (password = `<name>-demo-password`):

| user  | department | what they get through the very same hub |
|-------|------------|------------------------------------------|
| alice | sales      | order tools; only sales-owned orders, amounts redacted |
| bob   | finance    | order tools; every order, amounts included |
| carol | hr         | HR tools only; order backend is not even listed |

Sign in as `alice`, then in Claude Code ask something like *"list our orders"*.
Run `claude mcp logout mcp-hub` and `claude mcp login mcp-hub` as `carol` — the
tool list itself changes.

Automated checks of the whole chain (no browser needed):

```bash
python -m venv .venv && .venv/bin/pip install -r hub/requirements.txt
.venv/bin/python scripts/e2e.py                        # 17 assertions
.venv/bin/python scripts/simulate_claude_code_login.py # scripted CIMD/PKCE flow
```

## How the login actually works

This is the part worth teaching. Claude Code needs **zero client-side
configuration** beyond the hub URL:

1. Claude Code POSTs to `http://localhost:8000/mcp` with no token. The hub
   answers `401` with a `WWW-Authenticate` header pointing at its **Protected
   Resource Metadata** (RFC 9728).
2. That metadata names the authorization server — the Keycloak realm. Claude
   Code reads Keycloak's own metadata and sees
   `client_id_metadata_document_supported: true`.
3. Claude Code starts an authorization code flow with PKCE where the
   `client_id` is a URL: `https://claude.ai/oauth/claude-code-client-metadata`.
   Keycloak fetches that document and treats it as the client registration —
   this is **CIMD** (OAuth Client ID Metadata Documents), and it means *no
   client pre-registration and no dynamic-registration database writes*.
4. The user signs in (and approves a consent screen on first login), Keycloak
   redirects to Claude Code's loopback listener, Claude Code exchanges the
   code and stores the tokens. Refresh happens silently until the SSO session
   (24h here) expires.

`scripts/simulate_claude_code_login.py` performs these exact steps headlessly
and prints each one — read it side by side with this section.

### What ends up inside the token

The realm stamps two things into every access token via a default client scope
(`keycloak/realm-mcp-demo.json`):

```json
{
  "aud": "http://localhost:8000/mcp",     // audience = the hub resource
  "department": ["sales"],                 // from IdP group membership
  "preferred_username": "alice"
}
```

Keycloak does not yet honor the RFC 8707 `resource` parameter, so the audience
comes from an **audience mapper** instead — the pattern the Keycloak MCP guide
recommends. Both the hub and the backends require this exact `aud`, which is
what stops a token minted for some other app from being replayed here.

## Where each authorization decision lives

| decision | where | code |
|----------|-------|------|
| "Is this a valid corporate user?" | IdP (SSO, MFA, session lifetime) | `keycloak/realm-mcp-demo.json` |
| "May sales reach the HR MCP at all?" | Hub routing table | `hub/config.*.yaml` |
| "Which orders may alice see, with which fields?" | Order backend | `servers/order/server.py` |
| "What if someone bypasses the hub?" | Backends re-verify the JWT themselves | `servers/common/keycloak_auth.py` |

Two implementation styles are shown on purpose:

- **`hub/app.py`** writes the OAuth plumbing out by hand (metadata route, 401
  challenge, JWKS verification, contextvars) — read this to understand what a
  gateway must actually do. Routing is a ~10-line YAML table; the hub carries
  no session, no storage, no token exchange.
- **`servers/*/server.py`** use the MCP Python SDK's built-in resource-server
  support (`token_verifier=` + `auth=`) — read these for the idiomatic minimal
  version. The HR backend also re-checks the `department` claim in its tools:
  it refuses non-HR callers even if the hub is misconfigured or bypassed
  entirely. That refusal is asserted in `scripts/e2e.py`.

## Adapting this to production

- **Your IdP instead of Keycloak.** The hub and backends only need three
  values: issuer URL, JWKS URL, expected audience. Any OIDC IdP that can (a)
  put a department/group claim in the access token and (b) let a public PKCE
  client authenticate works the same way. If your IdP does not support CIMD,
  pre-register Claude Code as a public client with loopback redirect URIs and
  serve a small static registration response — or check whether it supports
  RFC 7591 dynamic registration.
- **Real backends.** Each backend needs ~30 lines: verify JWT against the IdP
  JWKS, read the claim, filter data. If a legacy backend cannot verify JWTs at
  all, terminate identity in front of it (see the AgentCore Gateway note
  below).
- **HTTPS everywhere**; loopback HTTP is only acceptable for the local demo.
- **Token lifetimes** are two Keycloak knobs in the realm file:
  `accessTokenLifespan` (3600) and `ssoSessionMaxLifespan` (86400) — i.e.
  hour-long tokens, one browser login per day.
- **The consent screen** appears because CIMD clients are identified by URL;
  Keycloak persists the approval per user, and you can disable the requirement
  through client policies if your security team prefers.
- **Keycloak's CIMD support is experimental** (`--features=cimd`, added in
  26.6). Track its promotion before depending on it in production, or fall
  back to pre-registration.
- **Managed alternative:** if you would rather not run the hub yourself,
  Amazon Bedrock AgentCore Gateway provides the same inbound CUSTOM_JWT
  verification as a service — but it cannot pass the user's JWT through to MCP
  targets (it swaps credentials via OAuth token exchange / API keys). Choose
  it when backends should *not* see IdP tokens; choose this hub pattern when
  they should.

## Application callers: HMAC key pair or an IAM entry

A human runs the SSO flow and presents a Bearer token. An *application*
(an agent platform calling on a user's behalf) has two ways to prove which
application it is, both carrying the acting user's token in
`X-MCPHUB-SSO-TOKEN`:

- **`MCPHUB-HMAC-SHA256`** — the application signs each request with an
  access/secret key pair the hub knows (`actors:` in config or `HUB_ACTORS`).
  Self-contained: works from anywhere, no AWS network in front of the hub.
- **IAM entry** — the hub sits behind a private Amazon API Gateway that
  authenticates the application with AWS IAM (SigV4) and forwards two headers
  the caller cannot set: `x-caller-arn` (the authenticated identity) and a
  shared secret only the gateway knows. The hub trusts `x-caller-arn` only on
  requests carrying that secret, and takes the actor from the assumed-role
  session name (`arn:aws:sts::…:assumed-role/<caller_role>/<actor>`). No key
  material to mint, distribute or rotate; the application's identity is its
  IAM role. Configure it with the `entry_identity` block (see
  `hub/config.local.yaml`) and `HUB_ENTRY_SECRET` in the environment, and
  make sure the listener is reachable only through the gateway's path.

Either way the department routing follows the token's claims; the
application scheme only adds "and we know which app sent it".

## Repository layout

```
├── docker-compose.yml            # the whole demo: IdP + hub + 2 backends
├── keycloak/realm-mcp-demo.json  # realm: users, groups, claims, CIMD policy, lifetimes
├── hub/
│   ├── app.py                    # the entire hub (~260 lines, hand-written OAuth)
│   └── config.*.yaml             # department → backend routing table
├── servers/
│   ├── common/keycloak_auth.py   # shared JWT verifier (SDK TokenVerifier)
│   ├── order/server.py           # MCP-1: row/field-level authorization demo
│   └── hr/server.py              # MCP-2: dept-gated + defense in depth
└── scripts/
    ├── e2e.py                    # 17 assertions across the whole chain
    └── simulate_claude_code_login.py  # Claude Code's OAuth flow, scripted
```

## Cleanup

```bash
claude mcp remove mcp-hub
docker compose down -v
```
