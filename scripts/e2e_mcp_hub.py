#!/usr/bin/env python3
"""End-to-end test of the MCP hub tool backend, driven over SSM.

Exercises the full production chain from the *calling application's* seat —
nothing here talks to the platform directly, everything runs on the demo-app
EC2 exactly as a customer application would:

    app EC2 ──SigV4 + robot token──► private service-entry API ──► entry ECS
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

Run after scripts/seed_mcp_hub_demo.py.
"""

import base64
import json
import os
import subprocess  # nosec B404 - fixed argv, no shell
import sys
import time

import boto3

TERRAFORM_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "terraform"
)
DEMO_CHANNEL_NAME = "mcp-hub-demo-app"

failures: list[str] = []


def check(tag: str, ok: bool, detail: str) -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {tag}: {detail}")
    if not ok:
        failures.append(tag)


def tf_output(name: str) -> str:
    proc = subprocess.run(  # nosec B603 - fixed argv, no shell
        ["terraform", f"-chdir={TERRAFORM_DIR}", "output", "-raw", name],
        capture_output=True, text=True, timeout=120, check=False,
    )
    value = proc.stdout.strip()
    if proc.returncode != 0 or not value or value == "null":
        raise SystemExit(f"terraform output {name} unavailable")
    return value


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


def find_channel_id() -> str:
    """The channel id isn't a Terraform output (it lives in the platform's
    registry) — look it up the same way the seed script created it."""
    import urllib.parse
    import urllib.request

    sm = boto3.client("secretsmanager")
    users_cfg = json.loads(
        sm.get_secret_value(SecretId="agent-platform/team-demo-users")["SecretString"]
    )
    issuer = users_cfg["issuer"]
    body = urllib.parse.urlencode({
        "grant_type": "password", "client_id": users_cfg["client_id"],
        "username": "admin", "password": users_cfg["users"]["admin"],
        "scope": "openid",
    }).encode()
    with urllib.request.urlopen(  # nosec B310 - fixed https base
        urllib.request.Request(f"{issuer}/protocol/openid-connect/token", data=body)
    ) as resp:
        token = json.load(resp)["access_token"]
    portal_url = tf_output("portal_url").rstrip("/")
    req = urllib.request.Request(
        f"{portal_url}/api/v1/channels", headers={"Authorization": f"Bearer {token}"}
    )
    with urllib.request.urlopen(req) as resp:  # nosec B310
        channels = json.load(resp)
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

    print(f"\n{'FAILED: ' + ', '.join(failures) if failures else 'all passed'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
