#!/usr/bin/env python3
"""stdio -> HTTP proxy for the platform's sandbox tools.

The sandbox tools are served by the platform backend (POST <portal>/api/v1/sandbox-mcp),
authenticated by a bearer token bound to this workbench session. Claude Code
could attach that as an HTTP MCP server with a static header, but the header
would then sit in /workspace/.mcp.json, which syncs to S3. So the kernel
attaches this stdio process instead and the token stays in a 0600 file under
/tmp, re-read per request (a reconnect rewrites it in place).

Each JSON-RPC line on stdin is POSTed as-is; the response (or nothing, for a
notification) goes to stdout. Standard library only.

Environment (set by contract-server per attachment):
    SANDBOX_MCP_URL         the backend endpoint
    SANDBOX_MCP_TOKEN_FILE  file holding the bearer token
    SANDBOX_MCP_SESSION     this workbench's runtime session id
"""

import json
import os
import sys
import urllib.error
import urllib.request

URL = os.environ.get("SANDBOX_MCP_URL", "")
TOKEN_FILE = os.environ.get("SANDBOX_MCP_TOKEN_FILE", "")
SESSION = os.environ.get("SANDBOX_MCP_SESSION", "")
# sandbox_run / sandbox_exec can legitimately take up to the job timeout
TIMEOUT_S = 1900


def log(msg: str) -> None:
    print(f"[sandbox-mcp] {msg}", file=sys.stderr, flush=True)


def token() -> str:
    try:
        with open(TOKEN_FILE, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def error_reply(line: str, message: str) -> str | None:
    try:
        mid = json.loads(line).get("id")
    except (ValueError, AttributeError):
        mid = None
    if mid is None:
        return None
    return json.dumps({"jsonrpc": "2.0", "id": mid, "error": {"code": -32000, "message": message}})


def forward(line: str) -> str | None:
    req = urllib.request.Request(  # noqa: S310 - URL is the platform backend, set by the kernel
        URL,
        data=line.encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {token()}",
            "X-Sandbox-Session": SESSION,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:  # noqa: S310
            body = resp.read().decode("utf-8")
            return body or None
    except urllib.error.HTTPError as e:
        detail = "workbench session ended or token invalid" if e.code == 401 else f"HTTP {e.code}"
        return error_reply(line, f"sandbox service: {detail}")
    except (urllib.error.URLError, TimeoutError) as e:
        return error_reply(line, f"sandbox service unreachable: {e}")


def main() -> int:
    if not URL or not TOKEN_FILE or not SESSION:
        log("SANDBOX_MCP_URL, SANDBOX_MCP_TOKEN_FILE and SANDBOX_MCP_SESSION are required")
        return 2
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        out = forward(line)
        if out:
            sys.stdout.write(out.strip() + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
