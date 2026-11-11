"""The SigV4 MCP client must report success only for a complete exchange.

verify.sh decides from this client's RESULT line whether the pod-to-pod path
works, so a client that says ok=true after a failed step makes the whole
verification green for a broken deployment. This test runs the client against
a local fake hub that fails one step at a time, every way a step can fail
(HTTP status, JSON-RPC error, wrong id, missing result, malformed body, tool
error, unparsable tool output), and requires ok=false, a nonzero exit and the
failing step named - plus the one fully successful exchange that must pass.

No AWS: the default credential chain is fed static dummy credentials through
the environment, which is all the signer needs to produce a signature the
fake hub does not check. Needs boto3 only. Run:
    python client/test_sigv4_mcp_client_adversarial.py
"""

from __future__ import annotations

import io
import json
import os
import sys
import threading
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

# Dummy static credentials for the signer, set before the client module (and
# boto3) is imported. Only the fake hub below ever sees a request.
os.environ.update({
    "AWS_ACCESS_KEY_ID": "AKIDEXAMPLE",
    "AWS_SECRET_ACCESS_KEY": "dummy-secret-for-offline-test",
    "AWS_DEFAULT_REGION": "ap-northeast-1",
    "AWS_EC2_METADATA_DISABLED": "true",
})
os.environ.pop("AWS_PROFILE", None)

import sigv4_mcp_client as client  # noqa: E402

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if condition else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))
    if not condition:
        failures.append(name)


WHOAMI = {"actor": "frontend-app", "caller_arn": "arn:aws:sts::" + "0" * 12 + ":assumed-role/r/s",
          "trust": "network", "user": None, "departments": []}

GOOD = {
    "initialize": (200, {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-06-18", "capabilities": {}}}),
    "notifications/initialized": (202, None),
    "tools/list": (200, {"jsonrpc": "2.0", "id": 2, "result": {"tools": [{"name": "hub__whoami"}]}}),
    "tools/call": (200, {"jsonrpc": "2.0", "id": 3, "result": {"content": [{"type": "text", "text": json.dumps(WHOAMI)}], "isError": False}}),
}


class FakeHub(BaseHTTPRequestHandler):
    plan: dict = {}
    seen: list = []

    def log_message(self, *args):  # quiet
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("content-length", "0"))) or b"{}")
        method = body.get("method")
        FakeHub.seen.append((method, "Authorization" in self.headers or "authorization" in self.headers))
        status, payload = FakeHub.plan.get(method, (500, {"error": "unexpected method"}))
        self.send_response(status)
        if payload is None:
            self.send_header("content-length", "0")
            self.end_headers()
            return
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


server = ThreadingHTTPServer(("127.0.0.1", 0), FakeHub)
threading.Thread(target=server.serve_forever, daemon=True).start()
# region_of() reads the region from an execute-api host name; the fake hub
# listens on localhost, so the client's one transport function is pointed at
# it. Signing still runs over the real URL.
URL = "https://abc123.execute-api.ap-northeast-1.amazonaws.com/mcp/mcp"
LOCAL = f"http://127.0.0.1:{server.server_address[1]}/mcp/mcp"

_post = client.post
client.post = lambda url, body, headers, timeout=30.0: _post(LOCAL if url == URL else url, body, headers, timeout)
client.boto3.client = lambda *a, **k: type("Sts", (), {"get_caller_identity": lambda self: {"Arn": "dummy"}})()


def run(plan: dict) -> tuple[int, dict]:
    FakeHub.plan, FakeHub.seen = plan, []
    out = io.StringIO()
    with redirect_stdout(out):
        try:
            rc = client.main(URL)
        except Exception as exc:  # the contract is a RESULT line, never a traceback
            print(f"CRASH {type(exc).__name__}: {exc}")
            rc = -1
    lines = [ln for ln in out.getvalue().splitlines() if ln.startswith("RESULT ")]
    return rc, (json.loads(lines[-1][7:]) if lines else {})


rc, res = run(GOOD)
check("a complete exchange is ok, exit 0", rc == 0 and res.get("ok") is True and res.get("actor") == "frontend-app", str(res))
check("every request was SigV4-signed", FakeHub.seen and all(signed for _, signed in FakeHub.seen), str(FakeHub.seen))
check("a 200 with no body is accepted for the notification",
      run({**GOOD, "notifications/initialized": (200, None)})[0] == 0)

STEP = {"initialize": "initialize", "notifications/initialized": "notifications/initialized",
        "tools/list": "tools/list", "tools/call": "tools/call hub__whoami"}
rpc_id = {"initialize": 1, "tools/list": 2, "tools/call": 3}

cases: list[tuple[str, str, tuple]] = []
for method in ("initialize", "tools/list", "tools/call"):
    i = rpc_id[method]
    cases += [
        (method, "HTTP 403", (403, {"Message": "denied"})),
        (method, "HTTP 500", (500, {"jsonrpc": "2.0", "id": i, "result": {}})),
        (method, "JSON-RPC error at 200", (200, {"jsonrpc": "2.0", "id": i, "error": {"code": -32603, "message": "boom"}})),
        (method, "result next to an error", (200, {"jsonrpc": "2.0", "id": i, "result": GOOD[method][1]["result"], "error": {"code": 1, "message": "x"}})),
        (method, "wrong JSON-RPC id", (200, {**GOOD[method][1], "id": i + 40})),
        (method, "no result", (200, {"jsonrpc": "2.0", "id": i})),
        (method, "result is not an object", (200, {"jsonrpc": "2.0", "id": i, "result": "ok"})),
        (method, "malformed JSON body", (200, b"{not json")),
        (method, "empty 202 instead of an answer", (202, None)),
    ]
cases += [
    ("notifications/initialized", "HTTP 403", (403, {"Message": "denied"})),
    ("notifications/initialized", "HTTP 500", (500, None)),
    ("notifications/initialized", "JSON-RPC error at 200", (200, {"jsonrpc": "2.0", "error": {"code": -32600, "message": "bad"}})),
    ("notifications/initialized", "malformed body at 200", (200, b"<html>error</html>")),
    ("notifications/initialized", "JSON-RPC error at 202", (202, {"jsonrpc": "2.0", "error": {"code": -32600, "message": "bad"}})),
    ("notifications/initialized", "malformed body at 202", (202, b"<html>error</html>")),
    ("notifications/initialized", "any JSON body at 202", (202, {"jsonrpc": "2.0", "result": {}})),
    ("notifications/initialized", "a literal null body at 202", (202, b"null")),
    ("notifications/initialized", "a literal null body at 200", (200, b"null")),
    ("tools/list", "tools is not a list", (200, {"jsonrpc": "2.0", "id": 2, "result": {"tools": "hub__whoami"}})),
    ("tools/list", "a null tool entry", (200, {"jsonrpc": "2.0", "id": 2, "result": {"tools": [None]}})),
    ("tools/list", "a tool entry without a string name", (200, {"jsonrpc": "2.0", "id": 2, "result": {"tools": [{"name": 3}]}})),
    ("tools/call", "tool reports isError", (200, {"jsonrpc": "2.0", "id": 3, "result": {"content": [{"type": "text", "text": "{}"}], "isError": True}})),
    ("tools/call", "no content", (200, {"jsonrpc": "2.0", "id": 3, "result": {"content": [], "isError": False}})),
    ("tools/call", "tool text is not JSON", (200, {"jsonrpc": "2.0", "id": 3, "result": {"content": [{"type": "text", "text": "hello"}], "isError": False}})),
]
for label, text in (("null", "null"), ("a list", "[]"), ("a number", "7"), ("an object without actor", '{"caller_arn": "x"}'),
                    ("an object without caller_arn", '{"actor": "frontend-app"}'), ("a non-string actor", '{"actor": 1, "caller_arn": "x"}')):
    cases.append(("tools/call", f"tool text is {label}",
                  (200, {"jsonrpc": "2.0", "id": 3, "result": {"content": [{"type": "text", "text": text}], "isError": False}})))

for method, how, response in cases:
    rc, res = run({**GOOD, method: response})
    check(f"{method}: {how} -> not ok, exit 1, step named",
          rc == 1 and res.get("ok") is False and res.get("failed_step") == STEP[method],
          f"rc={rc} {res}")

server.shutdown()
print(f"\n{'FAILED: ' + ', '.join(failures) if failures else 'all passed'}")
assert not failures, f"client checks failed: {failures}"
