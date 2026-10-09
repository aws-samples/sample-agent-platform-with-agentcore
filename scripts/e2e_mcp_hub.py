#!/usr/bin/env python3
"""End-to-end test of the MCP hub tool backend, driven over SSM.

Exercises the full production chain from the *calling application's* seat —
nothing here talks to the platform directly, everything runs on the demo-app
EC2 exactly as a customer application would:

    app EC2 ──SigV4 + robot token──► private service-entry API ──► entry pods (EKS)
      ──► published agent (AgentCore Runtime)
      ──MCPHUB-HMAC-SHA256 + forwarded SSO token──► hub EC2
      ──Bearer──► order / hr MCP backends

Checks:
    HUB-HEALTH   hub + both backends up (on-box)
    E2E-ALLOW    the agent answers an orders question via hub tools
                 (the app's service account is department=sales)
    E2E-DENY     an HR question does NOT leak HR data — the hub refuses the
                 hr backend for a sales-department identity and the agent
                 says so

Plus the *development* path — the same hub attached outside a published
agent, where requests sign as the shared ``dev-workbench`` Actor and the
forwarded token is the portal user's own:

    WB-ALLOW     Debug-console invoke as admin (department=sales) reaches the
                 order tools
    WB-USER      the same invoke as alice (department=hr) gets HR data — the
                 hub routes per user, not per attachment

Run after scripts/seed_mcp_hub_demo.py.
"""

import base64
import json
import os
import sys
import time

import boto3

DEMO_CHANNEL_NAME = "mcp-hub-demo-app"

failures: list[str] = []


def check(tag: str, ok: bool, detail: str) -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {tag}: {detail}")
    if not ok:
        failures.append(tag)


# The foundation (network, hub EC2s, IAM entry) lives in the operations
# project; its state is not ours to open. Everything these scripts need is
# published as the facts document (scripts/foundation_facts.py maps the names).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import foundation_facts  # noqa: E402

_FACTS: dict | None = None


def _facts() -> dict:
    global _FACTS
    if _FACTS is None:
        name = os.environ.get("QA_FACTS_PARAMETER", "/agent-platform/foundation")
        doc = json.loads(boto3.client("ssm").get_parameter(Name=name)["Parameter"]["Value"])
        if doc.get("schema_version") != 1:
            raise SystemExit(f"{name}: not a foundation facts document")
        _FACTS = doc
    return _FACTS


def _fact(name: str) -> str:
    present, value = foundation_facts.lookup(_facts(), name)
    if not present or value in (None, ""):
        raise SystemExit(
            f"foundation fact {name} is absent — is the operations stack applied with "
            f"enable_mcp_hub_demo=true (facts section mcp_hub)?"
        )
    return value if isinstance(value, str) else json.dumps(value)


def tf_output(name: str) -> str:
    """Kept under its old name: the values now come from the facts document."""
    return _fact(name)


def run_on(ssm, instance_id: str, script: str, timeout_s: int = 900) -> tuple[bool, str, str]:
    """Run a script on an instance via SSM. The script goes over base64 —
    quoting survives the shell → SSM JSON → remote shell relay intact."""
    encoded = base64.b64encode(script.encode()).decode()
    command_id = ssm.send_command(
        InstanceIds=[instance_id],
        DocumentName="AWS-RunShellScript",
        Parameters={
            "commands": [
                f"printf %s {encoded} | base64 -d > /tmp/e2e_step.sh",
                "bash /tmp/e2e_step.sh",
            ],
            "executionTimeout": [str(timeout_s)],
        },
    )["Command"]["CommandId"]
    deadline = time.time() + timeout_s + 60
    while time.time() < deadline:
        time.sleep(5)
        result = ssm.get_command_invocation(CommandId=command_id, InstanceId=instance_id)
        if result["Status"] in ("Success", "Failed", "Cancelled", "TimedOut"):
            return (
                result["Status"] == "Success",
                result.get("StandardOutputContent", ""),
                result.get("StandardErrorContent", ""),
            )
    return False, "", "ssm polling timed out"


def portal_token(username: str) -> str:
    """Password-grant login as a demo portal user (credentials seeded to
    Secrets Manager by seed_team_idp.py)."""
    import urllib.parse
    import urllib.request

    sm = boto3.client("secretsmanager")
    users_cfg = json.loads(
        sm.get_secret_value(SecretId="agent-platform/team-demo-users")["SecretString"]
    )
    body = urllib.parse.urlencode({
        "grant_type": "password", "client_id": users_cfg["client_id"],
        "username": username, "password": users_cfg["users"][username],
        "scope": "openid",
    }).encode()
    with urllib.request.urlopen(  # nosec B310 - fixed https base
        urllib.request.Request(
            f"{users_cfg['issuer']}/protocol/openid-connect/token", data=body
        )
    ) as resp:
        return json.load(resp)["access_token"]


def portal_api(path: str, token: str, payload: dict | None = None, timeout_s: int = 660):
    import urllib.request

    portal_url = tf_output("portal_url").rstrip("/")
    req = urllib.request.Request(
        f"{portal_url}{path}",
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"},
        method="POST" if payload is not None else "GET",
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # nosec B310
        return json.load(resp)


def find_channel_id() -> str:
    """The channel id isn't a Terraform output (it lives in the platform's
    registry) — look it up the same way the seed script created it."""
    channels = portal_api("/api/v1/channels", portal_token("admin"))
    channel = next((c for c in channels if c["name"] == DEMO_CHANNEL_NAME), None)
    if not channel:
        raise SystemExit(f"channel {DEMO_CHANNEL_NAME} not found — run seed_mcp_hub_demo.py")
    return channel["id"]


def invoke(ssm, app_instance: str, channel: str, message: str) -> tuple[bool, dict | None, str]:
    script = (
        f"python3.11 /opt/demo-app/invoke_agent.py "
        f"--channel {channel} --message {json.dumps(message)} --timeout 600"
    )
    ok, out, err = run_on(ssm, app_instance, script, timeout_s=700)
    record = None
    if out.strip():
        try:
            record = json.loads(out[out.index("{"):])
        except (ValueError, json.JSONDecodeError):
            pass
    return ok, record, (err or out)[-600:]


def main() -> int:
    hub_instance = tf_output("mcp_hub_instance_id")
    app_instance = tf_output("demo_app_instance_id")
    channel = find_channel_id()
    ssm = boto3.client("ssm")
    print(f"channel {channel}; hub {hub_instance}; app {app_instance}\n")

    ok, out, err = run_on(
        ssm, hub_instance,
        "curl -sf http://127.0.0.1:8000/healthz && echo && "
        "systemctl is-active mcp-hub mcp-backend-order mcp-backend-hr",
        timeout_s=60,
    )
    check("HUB-HEALTH", ok and out.count("active") >= 3, (out or err).strip().replace("\n", " · "))

    ok, record, tail = invoke(
        ssm, app_instance, channel,
        "Use your order tools to list recent orders and summarize them in one line.",
    )
    result_text = (record or {}).get("result", "")
    check(
        "E2E-ALLOW",
        ok and (record or {}).get("status") == "succeeded" and "ORD-" in result_text,
        f"status={(record or {}).get('status')} result={result_text[:160]!r}"
        if record else tail,
    )

    ok, record, tail = invoke(
        ssm, app_instance, channel,
        "Use the hr search_employee tool to look up Alice Zhang's level and location.",
    )
    result_text = (record or {}).get("result", "")
    leaked = "L5" in result_text and "Singapore" in result_text
    check(
        "E2E-DENY",
        ok and (record or {}).get("status") == "succeeded" and not leaked,
        f"status={(record or {}).get('status')} leaked={leaked} result={result_text[:160]!r}"
        if record else tail,
    )

    # ---- development path: hub attached outside a published agent ---------
    # (Debug console; the workbench uses the same resolution and the same
    # dev-workbench Actor). The forwarded token is the portal user's own, so
    # what the hub serves flips with the user, not with the attachment.
    hub_entry = next(
        (s for s in portal_api("/api/v1/ecosystem/mcp-servers", portal_token("admin"))
         if s.get("kind") == "mcp-hub"),
        None,
    )
    if not hub_entry:
        check("WB-ALLOW", False, "no mcp-hub registry entry — run seed_mcp_hub_demo.py")
    else:
        def wb_invoke(username: str, prompt: str) -> dict:
            try:
                return portal_api(
                    "/api/v1/kernels/agent-sdk/invoke", portal_token(username),
                    payload={"prompt": prompt, "max_turns": 8,
                             "mcp_server_ids": [hub_entry["id"]]},
                )
            except Exception as exc:  # noqa: BLE001 — recorded as a failure
                return {"ok": False, "result": f"invoke error: {exc}"}

        res = wb_invoke(
            "admin",
            "Use your order tools to list recent orders and summarize them in one line.",
        )
        check(
            "WB-ALLOW",
            bool(res.get("ok")) and "ORD-" in res.get("result", ""),
            f"ok={res.get('ok')} result={res.get('result', '')[:160]!r}",
        )

        res = wb_invoke(
            "alice",
            "Use the hr search_employee tool to look up Alice Zhang's level and location.",
        )
        check(
            "WB-USER",
            bool(res.get("ok")) and "L5" in res.get("result", ""),
            f"ok={res.get('ok')} result={res.get('result', '')[:160]!r}",
        )

    # ---- which application-auth path did the hub actually see? ------------
    # The hub logs the verified actor on every request: "entry actor verified"
    # on the IAM path (x-caller-arn behind the gateway's shared secret), "hmac
    # actor verified" on the HMAC path. The registry entry says which one this
    # deployment is on; the log must agree, and the other path must be silent.
    if hub_entry:
        auth = hub_entry.get("auth") or "hmac"
        want, other = ("entry", "hmac") if auth == "iam" else ("hmac", "entry")
        ok, out, err = run_on(
            ssm, hub_instance,
            "journalctl -u mcp-hub --since '-20 min' --no-pager -o cat | "
            f"grep -c '{want} actor verified' ; "
            "journalctl -u mcp-hub --since '-20 min' --no-pager -o cat | "
            f"grep -c '{other} actor verified' || true",
            timeout_s=60,
        )
        counts = [c.strip() for c in out.strip().splitlines() if c.strip()]
        seen = int(counts[0]) if counts and counts[0].isdigit() else -1
        stray = int(counts[1]) if len(counts) > 1 and counts[1].isdigit() else 0
        check(
            "HUB-AUTH-PATH",
            ok and seen >= 3 and stray == 0,
            f"registry auth={auth}: {seen} '{want} actor verified' lines, {stray} '{other}' lines in the last 20 min"
            + ("" if ok else f" ({err.strip()[:200]})"),
        )

    print(f"\n{'FAILED: ' + ', '.join(failures) if failures else 'all passed'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
