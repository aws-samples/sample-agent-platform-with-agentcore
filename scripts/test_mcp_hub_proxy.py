"""Integration test: kernel signing proxy ↔ hub-style HMAC verification.

Spins up a minimal in-process HTTP server that verifies MCPHUB-HMAC-SHA256
exactly the way the hub sample does (same verification module contract),
launches the real ``mcp_hub_proxy.py`` as a subprocess, and drives JSON-RPC
lines through it. No Keycloak, no AWS — this proves the wire format the
kernel emits is the wire format the hub accepts.

Usage:  python scripts/test_mcp_hub_proxy.py
"""

import hashlib
import hmac as hmac_mod
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "runtimes" / "agent-sdk-kernel" / "src"
sys.path.insert(0, str(SRC))

from mcphub_hmac import McpHubHmacSignature  # noqa: E402

AK, SK = "test-agent", "test-secret-key"
SSO = "service.account.token"

failures: list[str] = []


def verify(handler: BaseHTTPRequestHandler, body: bytes) -> str | None:
    """Hub-side check, condensed: rebuild the canonical request and compare."""
    auth = handler.headers.get("Authorization", "")
    if not auth.startswith(McpHubHmacSignature.ALGORITHM):
        return "wrong scheme"
    fields = dict(
        part.strip().partition("=")[::2]
        for part in auth.partition(" ")[2].split(",")
    )
    if fields.get("Credential") != AK:
        return "unknown actor"
    timestamp = int(handler.headers.get("X-MCPHUB-CLIENT-TIMESTAMP", "0"))
    if abs(int(time.time()) - timestamp) > 300:
        return "stale timestamp"
    sso = handler.headers.get("X-MCPHUB-SSO-TOKEN", "")
    if sso != SSO:
        return "sso token missing or wrong"
    canonical = McpHubHmacSignature.build_canonical_request(
        method="POST",
        url=handler.path,
        raw_body=body,
        sso_token=sso,
        content_type=handler.headers.get("Content-Type", ""),
        timestamp=timestamp,
        nonce=handler.headers.get("X-MCPHUB-CLIENT-NONCE", ""),
    )
    string_to_sign = "\n".join((
        McpHubHmacSignature.ALGORITHM,
        str(timestamp),
        hashlib.sha256(canonical.encode()).hexdigest(),
    ))
    expected = hmac_mod.new(SK.encode(), string_to_sign.encode(), hashlib.sha256).hexdigest()
    if not hmac_mod.compare_digest(expected, fields.get("Signature", "")):
        return "signature mismatch"
    return None


class Hub(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        reason = verify(self, body)
        if reason:
            payload = json.dumps({"error": "invalid_signature", "detail": reason}).encode()
            self.send_response(401)
        else:
            message = json.loads(body)
            if message.get("id") is None:
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            payload = json.dumps({
                "jsonrpc": "2.0", "id": message["id"],
                "result": {"echo": message.get("method", ""), "verified": True},
            }).encode()
            self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):  # quiet
        pass


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if condition else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not condition:
        failures.append(name)


def main() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Hub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}/mcp"

    env = {
        **os.environ,
        "MCPHUB_URL": url,
        "MCPHUB_SSO_TOKEN": SSO,
        "MCPHUB_ACCESS_KEY": AK,
        "MCPHUB_SECRET_KEY": SK,
    }
    env.pop("MCPHUB_CREDENTIALS_SECRET", None)
    proc = subprocess.Popen(
        [sys.executable, str(SRC / "mcp_hub_proxy.py")],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, env=env,
    )
    try:
        print("proxy ↔ HMAC hub round-trip:")

        # request → signed POST → verified → response comes back on stdout
        proc.stdin.write(json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}) + "\n")
        # notification → signed POST → 202, nothing on stdout
        proc.stdin.write(json.dumps(
            {"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        # second request, so we can tell the notification produced no output line
        proc.stdin.write(json.dumps(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "order__list", "arguments": {"q": "☕ quoted \"text\""}}}) + "\n")
        proc.stdin.flush()

        first = json.loads(proc.stdout.readline())
        second = json.loads(proc.stdout.readline())
        by_id = {m.get("id"): m for m in (first, second)}

        check("request 1 verified by the hub",
              by_id.get(1, {}).get("result", {}).get("verified") is True,
              json.dumps(by_id.get(1)))
        check("request 2 (non-ASCII + escaped quotes body) verified",
              by_id.get(2, {}).get("result", {}).get("verified") is True,
              json.dumps(by_id.get(2)))
        check("notification produced no response line", set(by_id) == {1, 2})

        # wrong secret → hub 401 → JSON-RPC error surfaced to the client
        proc.stdin.close()
        proc.wait(timeout=10)

        env_bad = {**env, "MCPHUB_SECRET_KEY": "not-the-secret"}
        bad = subprocess.run(
            [sys.executable, str(SRC / "mcp_hub_proxy.py")],
            input=json.dumps({"jsonrpc": "2.0", "id": 9, "method": "tools/list"}) + "\n",
            capture_output=True, text=True, env=env_bad, timeout=15,
        )
        error = json.loads(bad.stdout.strip())
        check("wrong secret → JSON-RPC error with the hub's 401 reason",
              error.get("error", {}).get("message", "").startswith("hub returned 401"),
              error.get("error", {}).get("message", ""))

        # missing SSO token → refuses to start
        env_missing = {k: v for k, v in env.items() if k != "MCPHUB_SSO_TOKEN"}
        missing = subprocess.run(
            [sys.executable, str(SRC / "mcp_hub_proxy.py")],
            input="", capture_output=True, text=True, env=env_missing, timeout=15,
        )
        check("missing SSO token refuses to start",
              missing.returncode == 2 and "MCPHUB_SSO_TOKEN" in missing.stderr,
              missing.stderr.strip().splitlines()[-1] if missing.stderr else "")
    finally:
        if proc.poll() is None:
            proc.kill()
        server.shutdown()

    print(f"\n{'FAILED: ' + ', '.join(failures) if failures else 'all passed'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
