#!/usr/bin/env python3
"""Acceptance suite for identity-aware gateway access (platform paths only).

Proves that *who is signed in* — not any demo-specific code path — determines
what an agent can reach: the same published agent, invoked by two users from
different teams, returns different results.

Checks (all through the ordinary platform APIs, no demo-only endpoint):
  /api/v1/me            identity + team claim per user
  /api/v1/gateways      gateway inventory: targets, credentials, interceptor
  /api/v1/gateways/{}/tools    catalog resolved with the caller's token
  /api/v1/ecosystem/mcp-servers   the gateway registered as an MCP entry
  /api/v1/agents        team-access-tester published with that MCP attached
  /api/v1/agents/{}/invoke   the SAME agent, invoked by alice and by carol
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

import boto3

PORTAL = os.environ.get("PORTAL_URL", "").rstrip("/")
if not PORTAL:
    raise SystemExit("set PORTAL_URL=https://<portal-distribution>.cloudfront.net")

sm = boto3.client("secretsmanager")
cfg = json.loads(sm.get_secret_value(SecretId="agent-platform/team-demo-users")["SecretString"])
ISSUER = cfg["issuer"]
GATEWAY_NAME = os.environ.get("GATEWAY_NAME", "agent-platform-team")

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))


def token(user):
    body = urllib.parse.urlencode(
        {
            "grant_type": "password",
            "client_id": "portal-web",
            "username": user,
            "password": cfg["users"][user],
            "scope": "openid",
        }
    ).encode()
    with urllib.request.urlopen(f"{ISSUER}/protocol/openid-connect/token", data=body) as r:
        return json.load(r)["access_token"]


def call(path, tok, payload=None, timeout=280):
    req = urllib.request.Request(
        f"{PORTAL}{path}",
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
        method="POST" if payload is not None else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, {"raw": e.read().decode(errors="replace")[:300]}


toks = {u: token(u) for u in ("alice", "carol")}

print("\n[identity]")
for user, team in (("alice", "team-a"), ("carol", "team-c")):
    st, me = call("/api/v1/me", toks[user])
    check(f"/me {user} -> {team}", st == 200 and me.get("teams") == [team], f"{st} {me.get('teams')}")

print("\n[gateway inventory]")
st, gws = call("/api/v1/gateways", toks["alice"])
gw = next((g for g in gws if g.get("name") == GATEWAY_NAME), {}) if st == 200 else {}
targets = {t["name"]: t for t in gw.get("targets", [])}
check("gateways lists the team gateway", st == 200 and bool(gw), f"{st} count={len(gws) if st==200 else '-'}")
check(
    "inbound authorizer is CUSTOM_JWT with the IdP discovery URL",
    gw.get("authorizer_type") == "CUSTOM_JWT" and "realms/agent-platform" in gw.get("discovery_url", ""),
    gw.get("authorizer_type", ""),
)
check(
    "REQUEST interceptor reported with header passing",
    any("REQUEST" in i["points"] and i["pass_request_headers"] for i in gw.get("interceptors", [])),
    str(gw.get("interceptors")),
)
check(
    "target enforcement inferred from outbound credential",
    targets.get("team-a", {}).get("enforcement") == "backend-app-layer"
    and targets.get("team-c", {}).get("enforcement") == "gateway-interceptor",
    ", ".join(f"{k}:{v['credential_type']}->{v['enforcement']}" for k, v in sorted(targets.items())),
)

print("\n[per-identity catalog]")
for user in ("alice", "carol"):
    st, cat = call(f"/api/v1/gateways/{gw.get('id')}/tools", toks[user])
    names = {t["name"] for t in cat.get("tools", [])}
    check(f"tools/list as {user} aggregates 3 targets", st == 200 and len(names) == 9, f"{st} n={len(names)}")

print("\n[registry + published agent]")
st, mcps = call("/api/v1/ecosystem/mcp-servers", toks["alice"])
entry = next((m for m in mcps if m["name"] == "team-apis-gateway"), {}) if st == 200 else {}
check(
    "gateway registered as an MCP server with an identity placeholder",
    entry.get("kind") == "url" and "{{user_token}}" in json.dumps(entry.get("headers", {})),
    json.dumps(entry.get("headers", {})),
)
st, agents = call("/api/v1/agents", toks["alice"])
agent = next((a for a in agents if a["name"] == "team-access-tester"), {}) if st == 200 else {}
check(
    "team-access-tester published with the gateway attached",
    "team-apis-gateway" in (agent.get("mcp_server_names") or []),
    f"v{agent.get('version')} mcp={agent.get('mcp_server_names')}",
)

print("\n[same agent, different identity]  (each invoke takes ~1-2 min)")
PROMPT = "Check my access across every team and report the table."
answers = {}
for user in ("alice", "carol"):
    st, res = call(f"/api/v1/agents/{agent.get('id')}/invoke", toks[user], {"prompt": PROMPT})
    text = (res.get("result") or json.dumps(res)).lower()
    # the agent writes team names freely ("Team C", "team-c"): normalize
    norm = text.replace("-", "").replace("_", "").replace(" ", "")
    answers[user] = text
    own = {"alice": "team-a", "carol": "team-c"}[user]
    others = {"alice": ["team-b", "team-c"], "carol": ["team-a", "team-b"]}[user]
    # own-team data present, other teams reported as denied, no foreign KPI leaked
    foreign_kpi = {"alice": ("campaigns", "gpu_hours"), "carol": ("deploys", "campaigns")}[user]
    check(
        f"agent invoked by {user}: reaches {own}",
        st == 200
        and own.replace("-", "") in norm
        and any(w in text for w in ("allowed", "success", "ok")),
        f"{st} {text[:110]}",
    )
    check(
        f"agent invoked by {user}: other teams denied, no data leak",
        st == 200
        and any(w in text for w in ("denied", "denied by", "forbidden", "authorization"))
        and not any(k in norm for k in (k2.replace("_", "") for k2 in foreign_kpi)),
        ", ".join(others),
    )

print("\n[the differentiator]")
check(
    "the two identities got materially different answers",
    bool(answers) and answers.get("alice") != answers.get("carol"),
)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("failed:", ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
