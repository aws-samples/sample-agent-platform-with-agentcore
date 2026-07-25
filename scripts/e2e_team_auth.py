#!/usr/bin/env python3
"""End-to-end test of the enterprise-SSO auth chain (team-auth demo).

Asserts the full identity propagation path with three users from different
teams (alice ∈ team-a, bob ∈ team-b, carol ∈ team-c — credentials seeded by
scripts/seed_team_idp.py):

    Keycloak login (team claim)
      → AgentCore Gateway (CUSTOM_JWT inbound)
        → team-a-api / team-b-api  (OBO token exchange out; the backends
          validate the token + enforce team themselves — app-layer authz)
        → team-c-api               (no SSO capability: the gateway's Lambda
          REQUEST interceptor enforces the team claim, and a static API key
          is injected outbound)
      → JWT-inbound AgentCore Runtime (team_demo_kernel)
        → gateway → team API, all with the same user token

Check matrix:
  IDP-*        alice/bob/carol password grant; token carries the right team
  GW-NOAUTH    gateway rejects unauthenticated MCP calls
  GW-OWN-*     each user calls their own team's tools
  GW-CROSS-*   calling another team's tool is denied — by the backend for
               team-a/b (app layer), by the gateway Lambda interceptor for
               team-c (catalog stays visible by design)
  DIRECT-*     the no-SSO team-c endpoint still rejects direct calls that
               lack the gateway-injected API key
  RT-*         runtime invoked with the user's token; agent reaches its team
               tool through the gateway; unauthenticated invoke rejected
  PORTAL-*     (when the portal runs in OIDC mode) /api accepts the token and
               /api/v1/team-demo/invoke works end to end

Usage:
    python3 scripts/e2e_team_auth.py [--skip-runtime] [--portal-url https://...]
"""

import argparse
import base64
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid

import boto3

USERS_SECRET = "agent-platform/team-demo-users"  # nosec B105 - secret name
SSM_PARAM = "/agent-platform/team-gateway"

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = ""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'✅' if ok else '❌'} {name}" + (f"  {detail}" if detail else ""))


def _post_json(url: str, payload: dict, headers: dict, timeout: int = 60):
    """POST JSON, return (status, parsed-body). Handles SSE-style MCP replies."""
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310
            raw = resp.read().decode()
            ctype = resp.headers.get("content-type", "")
            status = resp.status
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        ctype = e.headers.get("content-type", "") if e.headers else ""
        status = e.code
    if "text/event-stream" in ctype:
        for line in raw.splitlines():
            if line.startswith("data:"):
                raw = line[5:].strip()
                break
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, {"raw": raw[:400]}


def mcp_call(url: str, method: str, params: dict | None, token: str | None):
    headers = {"Accept": "application/json, text/event-stream"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return _post_json(
        url,
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
        headers,
    )


def mcp_initialize(url: str, token: str | None):
    return mcp_call(
        url,
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "e2e-team-auth", "version": "1.0"},
        },
        token,
    )


def claims_of(token: str) -> dict:
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


def password_grant(issuer: str, client_id: str, username: str, password: str) -> str:
    body = urllib.parse.urlencode(
        {
            "grant_type": "password",
            "client_id": client_id,
            "username": username,
            "password": password,
            "scope": "openid",
        }
    ).encode()
    req = urllib.request.Request(f"{issuer}/protocol/openid-connect/token", data=body)
    with urllib.request.urlopen(req) as resp:  # nosec B310
        return json.load(resp)["access_token"]


def list_all_tools(url: str, token: str) -> set[str]:
    """tools/list following pagination (the gateway pages per target)."""
    names: set[str] = set()
    cursor = None
    for _ in range(10):
        params = {"cursor": cursor} if cursor else {}
        status, resp = mcp_call(url, "tools/list", params, token)
        if status != 200 or "result" not in resp:
            break
        names |= {t["name"] for t in resp["result"].get("tools", [])}
        cursor = resp["result"].get("nextCursor")
        if not cursor:
            break
    return names


def call_text(resp: dict) -> str:
    """Flatten a tools/call response (result content or error) to text."""
    if "error" in resp:
        return json.dumps(resp["error"])
    content = (resp.get("result") or {}).get("content") or []
    return " ".join(c.get("text", "") for c in content if isinstance(c, dict)) or json.dumps(resp)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-runtime", action="store_true")
    parser.add_argument("--portal-url", default="")
    args = parser.parse_args()

    session = boto3.Session()
    region = session.region_name
    sm = session.client("secretsmanager")
    ssm = session.client("ssm")

    users_cfg = json.loads(sm.get_secret_value(SecretId=USERS_SECRET)["SecretString"])
    issuer, client_id = users_cfg["issuer"], users_cfg["client_id"]
    gw = json.loads(ssm.get_parameter(Name=SSM_PARAM)["Parameter"]["Value"])
    mcp_url = gw["mcp_url"]
    runtime_arn = gw.get("runtime_arn", "")
    print(f"issuer:   {issuer}")
    print(f"gateway:  {mcp_url}")
    print(f"runtime:  {runtime_arn or '(not deployed)'}\n")

    # ------------------------------- IdP --------------------------------
    tokens: dict[str, str] = {}
    teams = {"alice": "team-a", "bob": "team-b", "carol": "team-c"}
    obo_teams = {"alice": "team-a", "bob": "team-b"}  # app-layer-authz backends
    for user, team in teams.items():
        tokens[user] = password_grant(issuer, client_id, user, users_cfg["users"][user])
        c = claims_of(tokens[user])
        check(
            f"IDP {user}: token carries team={team}",
            team in (c.get("team") or []),
            f"team={c.get('team')} aud={c.get('aud')}",
        )

    # -------------------------- gateway: no auth ------------------------
    status, body = mcp_call(mcp_url, "tools/list", None, token=None)
    check("GW-NOAUTH: unauthenticated call rejected", status in (401, 403), f"status={status}")

    # Aggregated gateways may prefix tool names with the target name
    # (e.g. "team-a___team_a_whoami") — resolve names from tools/list.
    def find_tool(names: set[str], suffix: str) -> str | None:
        return next((n for n in names if n == suffix or n.endswith(suffix)), None)

    all_names = list_all_tools(mcp_url, tokens["alice"])
    check(
        "GW: tools/list aggregates all three teams' tools",
        find_tool(all_names, "team_a_get_report") is not None
        and find_tool(all_names, "team_b_get_report") is not None
        and find_tool(all_names, "team_c_get_report") is not None,
        f"tools={sorted(all_names)}",
    )

    # ------------------------ gateway: own team -------------------------
    for user, team in obo_teams.items():
        slug = team.replace("-", "_")
        tok = tokens[user]
        status, resp = mcp_call(
            mcp_url,
            "tools/call",
            {"name": find_tool(all_names, f"{slug}_whoami") or f"{slug}_whoami", "arguments": {}},
            tok,
        )
        text = call_text(resp)
        check(
            f"GW-OWN {user}: {slug}_whoami returns the IdP identity",
            status == 200 and user in text and team in text and "error" not in resp,
            text[:200],
        )

    # carol's backend has no SSO capability — its whoami cannot echo the IdP
    # identity; it reports that authz happened upstream in the interceptor.
    status, resp = mcp_call(
        mcp_url,
        "tools/call",
        {"name": find_tool(all_names, "team_c_whoami") or "team_c_whoami", "arguments": {}},
        tokens["carol"],
    )
    text = call_text(resp)
    check(
        "GW-OWN carol: team_c_whoami reachable (authz done by the interceptor)",
        status == 200 and "error" not in resp and "team-c" in text and "interceptor" in text,
        text[:200],
    )

    # ----------------------- gateway: cross team ------------------------
    for user, own in obo_teams.items():
        other = "team-b" if own == "team-a" else "team-a"
        slug = other.replace("-", "_")
        status, resp = mcp_call(
            mcp_url,
            "tools/call",
            {
                "name": find_tool(all_names, f"{slug}_get_report") or f"{slug}_get_report",
                "arguments": {},
            },
            tokens[user],
        )
        text = call_text(resp)
        denied = (
            status in (401, 403)
            or "error" in resp
            or (resp.get("result") or {}).get("isError")
            or "access denied" in text.lower()
            or "restricted" in text.lower()
        ) and "kpis" not in text and "campaigns" not in text
        check(
            f"GW-CROSS {user}: calling {other} tool is denied by the backend",
            bool(denied),
            f"status={status} {text[:140]}",
        )

    # team-c is gated by the gateway's Lambda REQUEST interceptor (the
    # backend itself has no SSO) — a non-member must be blocked there.
    status, resp = mcp_call(
        mcp_url,
        "tools/call",
        {
            "name": find_tool(all_names, "team_c_get_report") or "team_c_get_report",
            "arguments": {},
        },
        tokens["alice"],
    )
    text = call_text(resp)
    check(
        "GW-CROSS alice: team-c tool denied by the gateway interceptor",
        (status in (401, 403) or "error" in resp or (resp.get("result") or {}).get("isError"))
        and "interceptor" in text.lower()
        and "gpu_hours" not in text,
        f"status={status} {text[:160]}",
    )

    # ...and a member of the no-SSO team must still be blocked by the
    # app-layer backends of the SSO teams (both enforcement models coexist).
    status, resp = mcp_call(
        mcp_url,
        "tools/call",
        {
            "name": find_tool(all_names, "team_a_get_report") or "team_a_get_report",
            "arguments": {},
        },
        tokens["carol"],
    )
    text = call_text(resp)
    check(
        "GW-CROSS carol: team-a tool denied by the backend",
        (status in (401, 403) or "error" in resp or (resp.get("result") or {}).get("isError"))
        and "kpis" not in text
        and "deploys" not in text,
        f"status={status} {text[:140]}",
    )

    # --------------- direct access to the no-SSO backend ----------------
    # The team-c container checks only a static API key that the gateway
    # injects; a direct call without it must be rejected.
    team_api_base = gw.get("team_api_base") or issuer.rsplit("/realms/", 1)[0]
    status, resp = mcp_initialize(f"{team_api_base}/team-c/mcp", token=None)
    check(
        "DIRECT: team-c endpoint rejects calls without the gateway API key",
        status in (401, 403),
        f"status={status}",
    )

    # ------------------------------ runtime -----------------------------
    if runtime_arn and not args.skip_runtime:
        rt_url = (
            f"https://bedrock-agentcore.{region}.amazonaws.com/runtimes/"
            f"{urllib.parse.quote(runtime_arn, safe='')}/invocations?qualifier=DEFAULT"
        )

        def invoke(token: str | None, prompt: str, mcp: list[dict]):
            headers = {
                "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": f"e2e-team-auth-{uuid.uuid4().hex}",
            }
            if token:
                headers["Authorization"] = f"Bearer {token}"
            return _post_json(
                rt_url,
                {"prompt": prompt, "max_turns": 6, "mcp_servers": mcp},
                headers,
                timeout=280,
            )

        status, _ = invoke(None, "hello", [])
        check("RT-NOAUTH: unauthenticated invoke rejected", status in (401, 403), f"status={status}")

        def mcp_entry(token: str):
            return {
                "name": "team_gateway",
                "kind": "url",
                "target": mcp_url,
                "headers": {"Authorization": f"Bearer {token}"},
            }

        status, body = invoke(
            tokens["alice"],
            "Use the MCP tool whose name ends with 'team_a_whoami' and repeat "
            "its 'team' and 'preferred_username' fields verbatim in your answer.",
            [mcp_entry(tokens["alice"])],
        )
        answer = json.dumps(body)
        check(
            "RT-OWN alice: agent reached team-a API via gateway",
            status == 200 and "team-a" in answer and "alice" in answer,
            answer[:200],
        )

        status, body = invoke(
            tokens["alice"],
            "Use the MCP tool whose name ends with 'team_b_get_report'. If the "
            "call fails, quote the exact error message you received.",
            [mcp_entry(tokens["alice"])],
        )
        answer = json.dumps(body).lower()
        # the backend's 403 surfaces through the gateway as an MCP
        # authorization error; what matters is denial + no data leak
        denied_phrases = ("access denied", "restricted", "authorization error", "forbidden")
        check(
            "RT-CROSS alice: team-b tool denied end to end",
            status == 200
            and any(p in answer for p in denied_phrases)
            and "campaigns" not in answer,
            answer[:200],
        )

        # the no-SSO backend, end to end: carol's token rides browser →
        # runtime → gateway, the interceptor admits her, the API key opens
        # the backend
        status, body = invoke(
            tokens["carol"],
            "Use the MCP tool whose name ends with 'team_c_get_report' and "
            "repeat its 'team' field and one KPI name verbatim in your answer.",
            [mcp_entry(tokens["carol"])],
        )
        answer = json.dumps(body)
        check(
            "RT-OWN carol: agent reached the no-SSO team-c API via gateway",
            status == 200 and "team-c" in answer,
            answer[:200],
        )
    else:
        print("  (runtime checks skipped)")

    # ------------------------------ portal ------------------------------
    if args.portal_url:
        base = args.portal_url.rstrip("/")
        req = urllib.request.Request(f"{base}/api/v1/config")
        with urllib.request.urlopen(req) as resp:  # nosec B310
            cfg = json.load(resp)
        check("PORTAL: auth_mode is oidc", cfg.get("auth_mode") == "oidc", str(cfg))
        status, body = _post_json(
            f"{base}/api/v1/team-demo/invoke",
            {"prompt": "Call team_a_whoami and repeat the team field.", "max_turns": 6},
            {"Authorization": f"Bearer {tokens['alice']}"},
            timeout=60,
        )
        answer = json.dumps(body)
        check(
            "PORTAL alice: /team-demo/invoke works with the user token",
            status == 200 and "team-a" in answer,
            answer[:200],
        )
        status, body = _post_json(
            f"{base}/api/v1/team-demo/invoke",
            {"prompt": "hi"},
            {"Authorization": "Bearer not-a-token"},
            timeout=30,
        )
        check("PORTAL: bad token rejected", status == 401, f"status={status}")

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("failed checks:", ", ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
