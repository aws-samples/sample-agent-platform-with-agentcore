"""The gateway MCP client must report exactly what happened, and nothing kinder.

verify.sh reads this client's RESULT line to decide two opposite things: that
a permitted caller's tool call succeeded, and that an unpermitted caller's
call was refused by the gateway's policy. A client that says "ok" after a
failed step turns a broken deployment green; a client that says "denied" when
the request simply failed (a timeout, a 500, a garbled body) turns a broken
deployment into proof that the policy works. Both are tested here against a
local fake gateway that misbehaves one step at a time.

No AWS: the default credential chain is fed static dummy credentials through
the environment. Needs boto3 only (for the sigv4 mode). Run:
    python client/test_gateway_mcp_client_adversarial.py
"""

from __future__ import annotations

import io
import json
import os
import socket
import sys
import threading
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

os.environ.update({
    "AWS_ACCESS_KEY_ID": "AKIDEXAMPLE",
    "AWS_SECRET_ACCESS_KEY": "dummy-secret-for-offline-test",
    "AWS_DEFAULT_REGION": "ap-northeast-1",
    "AWS_EC2_METADATA_DISABLED": "true",
})
os.environ.pop("AWS_PROFILE", None)

import gateway_mcp_client as client  # noqa: E402

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if condition else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))
    if not condition:
        failures.append(name)


TOOL = "hub___hub__whoami"
WHOAMI = {"actor": "agentcore-gateway", "trust": "network", "via": "agentcore-gateway", "caller_arn": None,
          "user": "alice", "departments": ["team-a"]}
NEGOTIATED = "2025-03-26"  # older than what the client offers: the gateway's answer must win

GOOD = {
    "initialize": (200, {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": NEGOTIATED, "capabilities": {}}}),
    "notifications/initialized": (202, None),
    "tools/list": (200, {"jsonrpc": "2.0", "id": 2, "result": {"tools": [{"name": TOOL}]}}),
    "tools/call": (200, {"jsonrpc": "2.0", "id": 3, "result": {"content": [{"type": "text", "text": json.dumps(WHOAMI)}]}}),
}
# verbatim from a deployed gateway refusing a caller its Cedar policy does not permit
DENIED = {"jsonrpc": "2.0", "id": 3, "error": {"code": -32002, "message": "Tool Execution Denied: Tool call not allowed due to policy enforcement [No policy applies to the request (denied by default).]"}}
# ... and, from the same gateway, a call to a tool that does not exist: an error, not a refusal
UNKNOWN_TOOL = {"jsonrpc": "2.0", "id": 3, "error": {"code": -32602, "message": "Unknown tool: hub___no_such_tool"}}
NO_TOOLS = (200, {"jsonrpc": "2.0", "id": 2, "result": {"tools": []}})


class FakeGateway(BaseHTTPRequestHandler):
    plan: dict = {}
    seen: list = []
    hits: int = 0
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # quiet
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("content-length", "0")))
        try:
            method = json.loads(body).get("method")
        except ValueError:
            method = None
        FakeGateway.seen.append((method, self.headers.get("authorization", ""), self.headers.get("mcp-protocol-version", "")))
        answer = FakeGateway.plan.get(method, (500, None))
        if answer == "close":  # accept the request, answer nothing
            self.close_connection = True
            self.connection.shutdown(socket.SHUT_RDWR)
            return
        status, payload = answer[0], answer[1]
        extra = answer[2] if len(answer) > 2 else {}
        FakeGateway.hits += 1
        raw = payload if isinstance(payload, bytes) else (b"" if payload is None else json.dumps(payload).encode())
        self.send_response(status)
        self.send_header("content-type", extra.get("content-type", "application/json"))
        for k, v in extra.items():
            if k != "content-type":
                self.send_header(k, v)
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


server = ThreadingHTTPServer(("127.0.0.1", 0), FakeGateway)
threading.Thread(target=server.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{server.server_address[1]}/mcp"
# The client accepts gateway URLs only; the test points it at the fake by
# replacing the one function that enforces that, and checks the real one below.
REAL_REGION_OF = client.region_of
client.region_of = lambda url: "ap-northeast-1"
# The one call the client makes to AWS itself (STS, to log which role it runs
# as). Stubbed: this test sends nothing off the machine.
REAL_LOCAL_IDENTITY = client.local_identity
client.local_identity = lambda region: "arn:aws:sts::" + "0" * 12 + ":assumed-role/test-role/test-session"


def run(plan: dict, auth: str = "sigv4", token: str = "user.jwt.token\n", url: str = BASE) -> tuple[int, dict, str]:
    FakeGateway.plan, FakeGateway.seen = plan, []
    out = io.StringIO()
    argv, stdin = sys.argv, sys.stdin
    sys.argv, sys.stdin = ["gateway_mcp_client.py", "--auth", auth, url], io.StringIO(token)
    try:
        with redirect_stdout(out):
            try:
                rc = client.main()
            except SystemExit as exc:  # argparse / explicit exits
                rc = exc.code if isinstance(exc.code, int) else 1
    finally:
        sys.argv, sys.stdin = argv, stdin
    text = out.getvalue()
    lines = [ln[len("RESULT "):] for ln in text.splitlines() if ln.startswith("RESULT ")]
    return rc, (json.loads(lines[-1]) if lines else {}), text


def variant(method: str, answer) -> dict:
    plan = dict(GOOD)
    plan[method] = answer
    return plan


# ------------------------------------------------------------------ the two honest outcomes
rc, res, _ = run(GOOD)
check("a complete exchange: call ok, exit 0, the hub's answer reported", rc == 0 and res.get("call") == "ok" and res.get("whoami") == WHOAMI and res.get("tools") == [TOOL], str(res))
check("sigv4: every request carried a SigV4 Authorization for bedrock-agentcore",
      bool(FakeGateway.seen) and all(a.startswith("AWS4-HMAC-SHA256 ") and "/bedrock-agentcore/aws4_request" in a for _, a, _ in FakeGateway.seen), str(FakeGateway.seen[:1]))
versions = [v for _, _, v in FakeGateway.seen]
check("after initialize every request carries the version the gateway negotiated, not the one the client offered",
      versions[0] == client.PROTOCOL_VERSION and versions[1:] and all(v == NEGOTIATED for v in versions[1:]), str(versions))
rc, res, _ = run(GOOD, auth="bearer")
check("bearer: the token read from stdin is sent as the Bearer, and the exchange is ok",
      rc == 0 and res.get("call") == "ok" and all(a == "Bearer user.jwt.token" for _, a, _ in FakeGateway.seen), str(FakeGateway.seen[:1]))
rc, res, _ = run(GOOD, auth="none")
check("none: no Authorization header is sent", all(a == "" for _, a, _ in FakeGateway.seen), str(FakeGateway.seen[:1]))

check("a 200 with an empty body is accepted for the notification (some servers answer 200, not 202)",
      run(variant("notifications/initialized", (200, None)))[1].get("call") == "ok")
rc, res, _ = run({**variant("tools/call", (200, DENIED)), "tools/list": NO_TOOLS})
check("a policy refusal (JSON-RPC error on the call, no tool listed): denied, exit 3, no whoami",
      rc == 3 and res.get("call") == "denied" and res.get("whoami") is None and res.get("tools") == [], str(res))
check("the refusal the test replays is the one the client recognizes, by code and by wording",
      client.policy_refusal(200, DENIED) and client.POLICY_DENIED_CODE == -32002 and client.POLICY_DENIED_TEXT in DENIED["error"]["message"])

# ------------------------------------------------------------------ never "ok" for a failed step
for method, how, answer in (
    ("initialize", "HTTP 401", (401, {"error": "x"}, {"www-authenticate": 'Bearer resource_metadata="https://x/.well-known/oauth-protected-resource"'})),
    ("initialize", "HTTP 403", (403, {"message": "Forbidden"})),
    ("initialize", "HTTP 500", (500, None)),
    ("initialize", "a JSON-RPC error", (200, {"jsonrpc": "2.0", "id": 1, "error": {"code": -1, "message": "no"}})),
    ("initialize", "a body that is not JSON", (200, b"<html>blocked</html>")),
    ("initialize", "an empty 200", (200, None)),
    ("initialize", "a JSON list, not an object", (200, [1, 2])),
    ("initialize", "a null result", (200, {"jsonrpc": "2.0", "id": 1, "result": None})),
    ("initialize", "a result that is a string", (200, {"jsonrpc": "2.0", "id": 1, "result": "ok"})),
    ("initialize", "a result that is a list", (200, {"jsonrpc": "2.0", "id": 1, "result": []})),
    ("initialize", "a result beside an error", (200, {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": NEGOTIATED}, "error": {"code": -1, "message": "no"}})),
    ("notifications/initialized", "HTTP 500", (500, None)),
    ("notifications/initialized", "HTTP 403", (403, {"message": "Forbidden"})),
    ("notifications/initialized", "HTTP 400", (400, {"jsonrpc": "2.0", "error": {"code": -32600, "message": "bad"}})),
    ("notifications/initialized", "a 200 carrying a JSON-RPC error", (200, {"jsonrpc": "2.0", "error": {"code": -1, "message": "no"}})),
    ("notifications/initialized", "no answer at all", "close"),
    ("tools/list", "a null result", (200, {"jsonrpc": "2.0", "id": 2, "result": None})),
    ("tools/list", "a result that is a list", (200, {"jsonrpc": "2.0", "id": 2, "result": []})),
    ("tools/list", "a result beside an error", (200, {"jsonrpc": "2.0", "id": 2, "result": {"tools": []}, "error": {"code": -1, "message": "no"}})),
    ("tools/list", "HTTP 400 (unsupported protocol version)", (400, {"jsonrpc": "2.0", "id": 2, "error": {"code": -32600, "message": "Unsupported protocol version"}})),
    ("tools/list", "HTTP 500", (500, None)),
    ("tools/list", "tools is not a list", (200, {"jsonrpc": "2.0", "id": 2, "result": {"tools": "x"}})),
    ("tools/list", "no result", (200, {"jsonrpc": "2.0", "id": 2})),
    ("tools/list", "a body that is not JSON", (200, b"not json")),
):
    rc, res, _ = run(variant(method, answer))
    check(f"{method}: {how} -> not ok, exit 1, the call never reported as made",
          rc == 1 and res.get("call") == "skipped" and res.get("whoami") is None, str(res)[:200])
rc, res, _ = run(variant("initialize", (401, {"error": "x"}, {"www-authenticate": 'Bearer resource_metadata="https://x/.well-known/oauth-protected-resource"'})))
check("a 401 at initialize keeps the discovery pointer in detail (verify.sh checks for it)", "resource_metadata" in res.get("detail", ""), str(res))

# ------------------------------------------------------------------ never "denied" for a request that merely failed
for how, answer in (
    # operational failures that carry an error object: the cases a loose client turns into "Cedar refused"
    ("an internal error (-32603) on a 200", (200, {"jsonrpc": "2.0", "id": 3, "error": {"code": -32603, "message": "Internal error"}})),
    ("an unknown tool (-32602), as the gateway answers it", (200, UNKNOWN_TOOL)),
    ("invalid params (-32602)", (200, {"jsonrpc": "2.0", "id": 3, "error": {"code": -32602, "message": "Invalid params"}})),
    ("a throttling error object", (200, {"jsonrpc": "2.0", "id": 3, "error": {"code": -32000, "message": "Rate exceeded"}})),
    ("the refusal's code with another message", (200, {"jsonrpc": "2.0", "id": 3, "error": {"code": -32002, "message": "Resource not found"}})),
    ("the refusal's wording with another code", (200, {"jsonrpc": "2.0", "id": 3, "error": {"code": -32603, "message": "Tool Execution Denied: Tool call not allowed due to policy enforcement"}})),
    ("the refusal's code as a string", (200, {"jsonrpc": "2.0", "id": 3, "error": {"code": "-32002", "message": "Tool Execution Denied: policy enforcement"}})),
    ("the refusal with a message that is not a string", (200, {"jsonrpc": "2.0", "id": 3, "error": {"code": -32002, "message": ["policy enforcement"]}})),
    ("an empty error object", (200, {"jsonrpc": "2.0", "id": 3, "error": {}})),
    ("the exact refusal answered with 403 (the gateway answers 200)", (403, DENIED)),
    ("the exact refusal answered with 500", (500, DENIED)),
    ("a tool that ran and failed (isError)", (200, {"jsonrpc": "2.0", "id": 3, "result": {"isError": True, "content": [{"type": "text", "text": "no"}]}})),
    ("HTTP 500", (500, None)),
    ("HTTP 502", (502, b"Bad Gateway")),
    ("HTTP 401", (401, {"error": "expired"})),
    ("HTTP 429", (429, {"message": "throttled"})),
    ("an empty 200", (200, None)),
    ("a 200 that is not JSON", (200, b"<html>error</html>")),
    ("a 200 with no result and no error", (200, {"jsonrpc": "2.0", "id": 3})),
    ("a 200 whose result is not an object", (200, {"jsonrpc": "2.0", "id": 3, "result": "ok"})),
    ("a 200 whose error is a string, not a JSON-RPC error object", (200, {"jsonrpc": "2.0", "id": 3, "error": "boom"})),
    ("a 200 whose isError is the string 'true'", (200, {"jsonrpc": "2.0", "id": 3, "result": {"isError": "true", "content": []}})),
    ("no answer at all (connection closed)", "close"),
):
    for listing, plan in (("the tool listed", variant("tools/call", answer)), ("no tool listed", {**variant("tools/call", answer), "tools/list": NO_TOOLS})):
        rc, res, _ = run(plan)
        check(f"tools/call: {how}, {listing} -> error, exit 1, NOT denied",
              rc == 1 and res.get("call") == "error" and res.get("whoami") is None, str(res)[:200])

# ------------------------------------------------------------------ never "ok" for a call that did not really succeed
for how, answer in (
    ("empty content", (200, {"jsonrpc": "2.0", "id": 3, "result": {"content": []}})),
    ("tool text that is not JSON", (200, {"jsonrpc": "2.0", "id": 3, "result": {"content": [{"type": "text", "text": "hello"}]}})),
    ("tool text that is a JSON list", (200, {"jsonrpc": "2.0", "id": 3, "result": {"content": [{"type": "text", "text": "[1]"}]}})),
    ("content that is not a list", (200, {"jsonrpc": "2.0", "id": 3, "result": {"content": "x"}})),
):
    rc, res, _ = run(variant("tools/call", answer))
    check(f"tools/call: {how} -> error, exit 1, no whoami", rc == 1 and res.get("call") == "error" and res.get("whoami") is None, str(res)[:200])
rc, res, _ = run({**GOOD, "tools/list": (200, {"jsonrpc": "2.0", "id": 2, "result": {"tools": []}})})
check("a call that answers although the tool was not listed is not ok (listing and calling must agree)",
      rc == 1 and res.get("call") == "error" and res.get("whoami") is None, str(res)[:200])

# ------------------------------------------------------------------ an answer counts only for the request it answers
# ... but a rejection of the credential is the gateway's answer whatever id it
# carries. Verbatim from deployed gateways answering initialize (sent as id 1):
# the JWT authorizer answers with id 0, the IAM one with the request's id.
JWT_401 = (401, {"jsonrpc": "2.0", "id": 0, "error": {"code": -32001, "message": "Missing Bearer token"}},
           {"www-authenticate": 'Bearer error="invalid_token", resource_metadata="https://gw.example/.well-known/oauth-protected-resource"'})
IAM_401 = (401, {"jsonrpc": "2.0", "id": 1, "error": {"code": -32001, "message": "Authentication error - Invalid credentials"}})
for label, answer in (("the JWT gateway's 401 (its body carries id 0, not the request's)", JWT_401), ("the IAM gateway's 401 (its body carries the request's id)", IAM_401)):
    rc, res, _ = run(variant("initialize", answer), auth="none")
    check(f"{label} is reported as the 401 it is: initialize 401, call skipped, exit 1",
          rc == 1 and res.get("initialize") == 401 and res.get("call") == "skipped" and res.get("whoami") is None, str(res)[:220])
rc, res, _ = run(variant("initialize", JWT_401), auth="none")
check("... and the JWT gateway's 401 keeps the discovery pointer verify.sh looks for, though its body is not this request's",
      "resource_metadata" in res.get("detail", "") and "id_mismatch" in res, str(res)[:260])
for status in (403, 500):
    rc, res, _ = run(variant("initialize", (status, {"jsonrpc": "2.0", "id": 0, "error": {"code": -32001, "message": "no"}})))
    check(f"a {status} whose body carries another id keeps its status (the HTTP answer stands, the body does not)",
          rc == 1 and res.get("initialize") == status and res.get("call") == "skipped", str(res)[:200])
rc, res, _ = run(variant("tools/call", (403, {**DENIED, "id": 0})))
check("a non-200 answer to tools/call with another id is an error, never denied (neither its status nor its body is a policy refusal)",
      rc == 1 and res.get("call") == "error" and res.get("whoami") is None, str(res)[:200])


def with_id(payload: dict, new_id) -> dict:
    out = dict(payload)
    if new_id is ...:
        out.pop("id", None)
    else:
        out["id"] = new_id
    return out


for label, new_id in (("another id (999)", 999), ("the id as a string", "3"), ("the id as a float", 3.0), ("true", True), ("null", None), ("no id at all", ...)):
    # the refusal: with a mismatched id it must not count as "Cedar refused this call"
    rc, res, _ = run({**variant("tools/call", (200, with_id(DENIED, new_id))), "tools/list": NO_TOOLS})
    check(f"a policy refusal carrying {label} is not this call's refusal: error, exit 1, NOT denied",
          rc == 1 and res.get("call") == "error" and res.get("whoami") is None and "id_mismatch" in res, str(res)[:220])
    # the success: with a mismatched id it must not count as this call's result
    rc, res, _ = run(variant("tools/call", (200, with_id(GOOD["tools/call"][1], new_id))))
    check(f"a tool result carrying {label} is not this call's result: error, exit 1, NOT ok",
          rc == 1 and res.get("call") == "error" and res.get("whoami") is None and "id_mismatch" in res, str(res)[:220])
for method, good_id in (("initialize", 1), ("tools/list", 2)):
    rc, res, _ = run(variant(method, (200, with_id(GOOD[method][1], 999))))
    check(f"{method}: an answer with another id -> not ok, exit 1, the call never reported as made",
          rc == 1 and res.get("call") == "skipped" and res.get("whoami") is None and "id_mismatch" in res, str(res)[:220])
rc, res, _ = run(GOOD)
check("the matching ids of a real exchange are accepted, and leave no id_mismatch", rc == 0 and res.get("call") == "ok" and "id_mismatch" not in res, str(res)[:200])

# ------------------------------------------------------------------ the credential goes to the URL it was given, and nowhere else
class Elsewhere(BaseHTTPRequestHandler):
    """Another host. Whatever reaches it is a leak."""
    got: list = []
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _record(self):
        n = int(self.headers.get("content-length", "0") or 0)
        Elsewhere.got.append((self.command, self.headers.get("authorization", ""), self.rfile.read(n) if n else b""))
        raw = json.dumps(GOOD["initialize"][1]).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    do_POST = do_GET = _record


elsewhere = ThreadingHTTPServer(("127.0.0.1", 0), Elsewhere)
threading.Thread(target=elsewhere.serve_forever, daemon=True).start()
ELSEWHERE = f"http://127.0.0.1:{elsewhere.server_address[1]}/collect"
for auth in ("bearer", "sigv4"):
    for status in (301, 302, 303, 307, 308):
        for method in ("initialize", "tools/list", "tools/call"):
            Elsewhere.got = []
            rc, res, _ = run(variant(method, (status, None, {"location": ELSEWHERE})), auth=auth)
            check(f"{auth}: a {status} on {method} is not followed - nothing reaches the other host, and the run is not ok or denied",
                  not Elsewhere.got and rc == 1 and res.get("call") in ("skipped", "error") and res.get("whoami") is None,
                  f"other host got {[(m, a[:16]) for m, a, _ in Elsewhere.got]}; {str(res)[:120]}")
Elsewhere.got = []
rc, res, _ = run(variant("initialize", (302, None, {"location": BASE})), auth="bearer")
check("a redirect back to the same gateway is not followed either (one request, one answer)",
      rc == 1 and res.get("initialize") == 302 and len([m for m, _, _ in FakeGateway.seen if m == "initialize"]) == 1, str(res)[:160])
import urllib.request as _ur  # noqa: E402
handlers = [type(h).__name__ for h in client._OPENER.handlers]
check("the client's opener has its own redirect handler and no default one",
      "_NoRedirect" in handlers and "HTTPRedirectHandler" not in handlers, str(handlers))
check("... and consults no proxy from the environment",
      not any(isinstance(h, _ur.ProxyHandler) and h.proxies for h in client._OPENER.handlers), str(handlers))
elsewhere.shutdown()

# ------------------------------------------------------------------ no HTTP answer: a RESULT line, never a traceback
for method in ("initialize", "tools/list"):
    rc, res, text = run(variant(method, "close"))
    check(f"{method}: no answer at all -> a RESULT line, exit 1, the transport error kept",
          rc == 1 and bool(res) and res.get("call") == "skipped" and bool(res.get("transport_error")), (str(res) or text)[:200])
dead = socket.socket()
dead.bind(("127.0.0.1", 0))
port = dead.getsockname()[1]
dead.close()  # nothing listens here now
rc, res, text = run(GOOD, url=f"http://127.0.0.1:{port}/mcp")
check("connection refused -> a RESULT line, exit 1, the transport error kept", rc == 1 and bool(res.get("transport_error")) and res.get("call") == "skipped", (str(res) or text)[:200])

# ------------------------------------------------------------------ inputs
rc, res, _ = run(GOOD, auth="bearer", token="\n")
check("bearer with an empty token on stdin: refuses to run, sends nothing", rc != 0 and not FakeGateway.seen, str((rc, FakeGateway.seen)))
for name, url in (
    ("another host", "https://evil.example.com/mcp"),
    ("http", "http://gw-abc.gateway.bedrock-agentcore.ap-northeast-1.amazonaws.com/mcp"),
    ("another path", "https://gw-abc.gateway.bedrock-agentcore.ap-northeast-1.amazonaws.com/other"),
    ("userinfo", "https://a@gw-abc.gateway.bedrock-agentcore.ap-northeast-1.amazonaws.com/mcp"),
    ("a look-alike suffix", "https://gw-abc.gateway.bedrock-agentcore.ap-northeast-1.amazonaws.com.evil.example/mcp"),
    ("a trailing newline", "https://gw-abc.gateway.bedrock-agentcore.ap-northeast-1.amazonaws.com/mcp\n"),
):
    try:
        REAL_REGION_OF(url)
        refused = False
    except SystemExit:
        refused = True
    check(f"the real URL check refuses {name}: credentials go to an AgentCore gateway only", refused)
check("the real URL check accepts a gateway URL and reads its Region",
      REAL_REGION_OF("https://gw-abc123.gateway.bedrock-agentcore.ap-northeast-1.amazonaws.com/mcp") == "ap-northeast-1")

# the diagnostic STS call must not be able to end the run
class _Boom(Exception):
    pass


import boto3  # noqa: E402
from botocore.exceptions import EndpointConnectionError  # noqa: E402

real_client = boto3.client


def failing_sts(service, *a, **k):
    class Sts:
        def get_caller_identity(self):
            raise EndpointConnectionError(endpoint_url="https://sts.invalid")
    return Sts() if service == "sts" else real_client(service, *a, **k)


boto3.client = failing_sts
try:
    arn = REAL_LOCAL_IDENTITY("ap-northeast-1")
finally:
    boto3.client = real_client
check("the identity lookup failing is reported as unknown, not raised (it is a log line, not a verdict)",
      arn.startswith("unknown (") and "EndpointConnectionError" in arn, arn)

rc, res, _ = run(GOOD)
check("after all of that a complete exchange is ok again, with no transport_error", rc == 0 and res.get("call") == "ok" and "transport_error" not in res, str(res))

server.shutdown()
print(f"\n{'FAILED: ' + ', '.join(failures) if failures else 'all passed'}")
assert not failures, f"gateway client checks failed: {failures}"
