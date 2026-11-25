"""verify.sh must fail every check whose evidence is not the evidence it names.

verify.sh decides from the client's RESULT records (and a few other answers)
that the deployment refuses what it should: a Cedar refusal, a resource-policy
refusal before Cedar (group 9's REFUSED), a security group dropping the
connection (group 9's SG_DROPPED). A verdict that accepts a near miss turns a
broken deployment green: a DNS failure looks like a dropped connection, an
unrelated 403 (a WAF, a proxy) looks like the gateway's resource policy, and a
call that succeeded where it must be refused is the one result that must
never pass.

This runs the real verify.sh, copied into a scratch directory, with every
outside command replaced by a stand-in (terraform, kubectl, aws, curl, the
token script and the local client) that answers from a "world" this test
writes. The good world passes every check; each forged world changes one
answer and must fail exactly the checks it names, and no other. The transport
errors and the 403s are not written by hand: the real gateway_mcp_client.py
produces them here, against a local fake gateway (a read timeout, a refused
connection, a failed name lookup, a TLS handshake against plain HTTP), so the
test also breaks when the client's RESULT shape drifts from what verify.sh
reads.

The forged records are near misses in value (another status, another error,
a tool list where none may be, the right words on the wrong status). Their
types are the client's to keep (an int status, a string detail: its own
test, client/test_gateway_mcp_client_adversarial.py, pins the RESULT line),
so a record that differs from a real one only in type (403.0 for 403) is not
a case here, and neither is a forged copy of a whole real record: verify.sh
reads the client's last RESULT line, and the client prints one.

No AWS, no cluster, no network. Run:
    python services/mcp-hub-demo/deploy/agentcore-gateway-entry/test_verify_adversarial.py
"""

from __future__ import annotations

import copy
import io
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "client"))
import gateway_mcp_client as client  # noqa: E402

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if condition else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))
    if not condition:
        failures.append(name)


# ------------------------------------------------------------------ records from the real client
TOOL = "hub___hub__whoami"


class FakeGateway(BaseHTTPRequestHandler):
    plan: dict = {}
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("content-length", "0")))
        method = json.loads(body).get("method")
        status, payload = FakeGateway.plan.get(method, (500, None))
        raw = b"" if payload is None else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


fake = ThreadingHTTPServer(("127.0.0.1", 0), FakeGateway)
threading.Thread(target=fake.serve_forever, daemon=True).start()
FAKE = f"127.0.0.1:{fake.server_address[1]}"

client.region_of = lambda url: "ap-northeast-1"  # the fake is not a gateway URL
_real_post = client.post


def run_client(url: str, plan: dict | None = None, timeout: float = 60.0) -> str:
    """What the real client prints (step lines and its RESULT line) for one run."""
    FakeGateway.plan = plan or {}
    client.post = lambda u, b, h, timeout_=timeout: _real_post(u, b, h, timeout=timeout_)
    out, argv, stdin = io.StringIO(), sys.argv, sys.stdin
    sys.argv, sys.stdin = ["gateway_mcp_client.py", "--auth", "none", url], io.StringIO("")
    try:
        with redirect_stdout(out):
            try:
                client.main()
            except SystemExit:
                pass
    finally:
        sys.argv, sys.stdin, client.post = argv, stdin, _real_post
    return out.getvalue()


def result_of(text: str) -> dict:
    lines = [ln[len("RESULT "):] for ln in text.splitlines() if ln.startswith("RESULT ")]
    return json.loads(lines[-1]) if lines else {}


WHOAMI_GW = {"actor": "agentcore-gateway", "user": None, "caller_arn": None, "departments": []}
whoami_text = lambda w: {"content": [{"type": "text", "text": json.dumps(w)}]}  # noqa: E731
INIT_OK = (200, {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-06-18", "capabilities": {}}})
NOTIFY_OK = (202, None)
LISTED = (200, {"jsonrpc": "2.0", "id": 2, "result": {"tools": [{"name": TOOL}]}})
NONE_LISTED = (200, {"jsonrpc": "2.0", "id": 2, "result": {"tools": []}})
CEDAR_DENIED = (200, {"jsonrpc": "2.0", "id": 3, "error": {"code": -32002, "message": "Tool Execution Denied: Tool call not allowed due to policy enforcement [No policy applies to the request (denied by default).]"}})


def exchange(whoami: dict | None = None, listed=LISTED, call=None) -> dict:
    return {"initialize": INIT_OK, "notifications/initialized": NOTIFY_OK, "tools/list": listed,
            "tools/call": call or (200, {"jsonrpc": "2.0", "id": 3, "result": whoami_text(whoami or WHOAMI_GW)})}


GW = f"http://{FAKE}/mcp"
REC: dict[str, str] = {}
REC["app_ok"] = run_client(GW, exchange(WHOAMI_GW))
REC["alice_ok"] = run_client(GW, exchange({"actor": "agentcore-gateway", "user": "alice", "caller_arn": None, "departments": ["team-a"]}))
REC["cedar_denied"] = run_client(GW, exchange(listed=NONE_LISTED, call=CEDAR_DENIED))
REC["hub_denied"] = run_client(GW, exchange(call=(200, {"jsonrpc": "2.0", "id": 3, "result": {"isError": True, "content": [
    {"type": "text", "text": "hub policy: departments ['team-c'] may not call tools here (allowed: ['team-a'])"}]}})))
REC["unsigned_403"] = run_client(GW, {"initialize": (403, {"message": "Missing Authentication Token"})})
REC["jwt_401"] = run_client(GW, {})  # replaced below: a 401 with a header
# the resource policy refusing before Cedar, as a deployed gateway answers it
REC["policy_refused"] = run_client(GW, {"initialize": (403, {"jsonrpc": "2.0", "id": 1, "error": {"code": -32002, "message": "Insufficient permissions"}})})
# 403s that are not that refusal
REC["unrelated_403"] = run_client(GW, {"initialize": (403, {"message": "Forbidden"})})
REC["cedar_wording_403"] = run_client(GW, {"initialize": (403, CEDAR_DENIED[1] | {"id": 1})})
REC["policy_401"] = run_client(GW, {"initialize": (401, {"jsonrpc": "2.0", "id": 1, "error": {"code": -32002, "message": "Insufficient permissions"}})})
REC["server_500"] = run_client(GW, {"initialize": (500, {"message": "Internal error"})})


class Jwt401(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("content-length", "0")))
        self.send_response(401)
        self.send_header("www-authenticate", 'Bearer resource_metadata="https://gw.example/.well-known/oauth-protected-resource"')
        self.send_header("content-length", "0")
        self.end_headers()


jwt = ThreadingHTTPServer(("127.0.0.1", 0), Jwt401)
threading.Thread(target=jwt.serve_forever, daemon=True).start()
REC["jwt_401"] = run_client(f"http://127.0.0.1:{jwt.server_address[1]}/mcp")

# transport errors: a connection that is accepted and never answered (the
# client's timeout, as a security group dropping packets ends), a refused
# connection, a failed name lookup and a TLS handshake against plain HTTP
silent = socket.socket()
silent.bind(("127.0.0.1", 0))
silent.listen(8)  # never accepted: the client's request is never answered
REC["timeout"] = run_client(f"http://127.0.0.1:{silent.getsockname()[1]}/mcp", timeout=0.5)
dead = socket.socket()
dead.bind(("127.0.0.1", 0))
dead_port = dead.getsockname()[1]
dead.close()
REC["refused"] = run_client(f"http://127.0.0.1:{dead_port}/mcp")
_real_gai = socket.getaddrinfo


def no_dns(host, *a, **k):
    if host == "gw.invalid":
        raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
    return _real_gai(host, *a, **k)


socket.getaddrinfo = no_dns
try:
    REC["dns"] = run_client("http://gw.invalid/mcp")
finally:
    socket.getaddrinfo = _real_gai
REC["tls"] = run_client(f"https://{FAKE}/mcp")
silent.close()

for name, expect in (("timeout", "TimeoutError"), ("refused", "ConnectionRefusedError"), ("dns", "gaierror"), ("tls", "SSL")):
    r = result_of(REC[name])
    check(f"the real client reports a {name} as initialize 0, call skipped, transport_error {expect}...",
          r.get("initialize") == 0 and r.get("call") == "skipped" and r.get("tools") is None
          and str(r.get("transport_error", "")).startswith(expect), str(r)[:160])
r = result_of(REC["policy_refused"])
check("the real client keeps the resource-policy refusal's body in detail (verify.sh reads -32002 there)",
      r.get("initialize") == 403 and "-32002" in r.get("detail", "") and "Insufficient permissions" in r.get("detail", ""), str(r)[:160])
for name in ("app_ok", "alice_ok"):
    check(f"the real client reports {name} as call ok", result_of(REC[name]).get("call") == "ok", str(result_of(REC[name]))[:120])
check("the real client reports a Cedar refusal as denied with no tool listed",
      result_of(REC["cedar_denied"]).get("call") == "denied" and result_of(REC["cedar_denied"]).get("tools") == [])

fake.shutdown()
jwt.shutdown()


def forged(**r) -> str:
    """A RESULT line written by hand (a record the client would not print, or a forgery)."""
    base = {"initialize": None, "tools": None, "call": "skipped", "whoami": None, "detail": ""}
    return "RESULT " + json.dumps({**base, **r})


# ------------------------------------------------------------------ the world verify.sh sees
ARN = "arn:aws:iam::ACCOUNT:role/"
IAM_URL = "https://iam-gw.gateway.bedrock-agentcore.ap-northeast-1.amazonaws.com/mcp"
JWT_URL = "https://jwt-gw.gateway.bedrock-agentcore.ap-northeast-1.amazonaws.com/mcp"
TF = {
    "region": "ap-northeast-1", "hub_cluster_name": "hub", "caller_cluster_name": "caller",
    "hub_namespace": "mcp-hub", "caller_namespace": "callers", "iam_gateway_url": IAM_URL, "jwt_gateway_url": JWT_URL,
    "allowed_teams": ["team-a", "team-c"], "hub_allowed_teams": ["team-a"],
    "hub_endpoint": "https://hub.internal.example/mcp", "hub_node_port": 30443,
    "coder_role_arn": ARN + "coder", "iam_gateway_arn": "arn:gw:iam", "jwt_gateway_arn": "arn:gw:jwt",
    "coder_url": "https://coder.example", "coder_alb_dns_name": "10.0.9.9",
}
GOOD = {
    "tf": TF,
    "tokens": {"alice": "tok-alice", "bob": "tok-bob", "carol": "tok-carol"},
    "clients": {
        # key: pod|auth|url|token ("local" is this machine)
        f"static-app|sigv4|{IAM_URL}|": REC["app_ok"],
        f"intruder|sigv4|{IAM_URL}|": REC["cedar_denied"],
        f"ws-probe|bearer|{JWT_URL}|tok-alice": REC["alice_ok"],
        f"ws-probe|bearer|{JWT_URL}|tok-bob": REC["cedar_denied"],
        f"ws-probe|bearer|{JWT_URL}|tok-carol": REC["hub_denied"],
        f"static-app|none|{IAM_URL}|": REC["unsigned_403"],
        f"ws-probe|none|{JWT_URL}|": REC["jwt_401"],
        f"static-app|bearer|{IAM_URL}|tok-carol": REC["unsigned_403"],
        f"static-app|bearer|{IAM_URL}|tok-bob": REC["unsigned_403"],  # group 4b skipped: bob's token is the last
        f"static-app|bearer|{JWT_URL}|tok-alice": REC["timeout"],      # 9a
        f"local|bearer|{JWT_URL}|tok-alice": REC["policy_refused"],     # 9b
        f"local|sigv4|{IAM_URL}|": REC["policy_refused"],               # 9c
        f"ws-probe|none|{IAM_URL}|": REC["timeout"],                    # 9d
    },
    "logs": "WARNING gateway entry refused tool hub__whoami: user=carol hub policy: ...",
    "pod_ip": "10.0.1.10", "host_ip": "10.0.1.20",
    "probe": {"hub.internal.example:443": "blocked: TimeoutError 10.0.2.5",
              "10.0.1.10:8000": "blocked: TimeoutError 10.0.1.10",
              "10.0.1.20:30443": "blocked: ConnectionRefusedError 10.0.1.20"},
    "aws_sim": {"arn:gw:iam": "implicitDeny", "arn:gw:jwt": "implicitDeny"},
    "waf_acl": {"WebACL": {"DefaultAction": {"Block": {}}, "Rules": [
        {"Action": {"Allow": {}}, "Statement": {"IPSetReferenceStatement": {"ARN": "arn:waf:ipset"}}}]}},
    "curl": "200",
}

N = {  # every check of the good world, by name
    "g1_ok": "SigV4 with the pod's IRSA role: the tool is listed and the call succeeds",
    "g1_who": "the hub sees the gateway, not the calling application (no caller ARN, no user)",
    "g2_init": "IAM admits the intruder to the gateway (it holds InvokeGateway)",
    "g2_cedar": "Cedar permits it nothing: no tool listed, the call refused",
    "g3_tok": "obtained an access token for alice",
    "g3_ok": "the user's token: the tool is listed and the call succeeds",
    "g3_user": "the hub still knows the user (token exchanged by the gateway)",
    "g4_tok": "obtained an access token for bob",
    "g4_cedar": "a valid token of another team: no tool listed, the call refused by Cedar",
    "g4b_tok": "obtained an access token for carol",
    "g4b_hub": "the gateway lists the tool, the hub refuses the call with its own rule",
    "g4b_log": "the hub logged the refusal (the request did reach it)",
    "g5_unsigned": "IAM gateway refuses an unsigned request",
    "g5_jwt401": "JWT gateway answers 401 with the discovery pointer an MCP client signs in from",
    "g5_bearer": "IAM gateway refuses a user's bearer token",
    "g6_iam": "coder role, InvokeGateway on the iam gateway",
    "g6_jwt": "coder role, InvokeGateway on the jwt gateway",
    "g7_nlb": "a pod in the hub VPC cannot connect to the hub NLB",
    "g7_pod": "a pod on the same node cannot connect to the hub pod",
    "g7_nodeport": "a pod cannot connect to the hub's NodePort",
    "g8_reach": "the operator reaches Coder through CloudFront",
    "g8_waf": "the Coder WAF blocks by default and allows one IP set",
    "g8_alb": "the Coder ALB is internal (private addresses only)",
    "g9_tok": "obtained an access token for alice (for group 9)",
    "g9a": "a valid user token from the app subnet cannot reach the JWT gateway",
    "g9b": "the same valid token from outside the VPC is refused by the JWT gateway's resource policy",
    "g9c": "a valid SigV4 signature from outside the VPC is refused by the IAM gateway's resource policy",
    "g9d": "the workspace subnet cannot reach the IAM gateway",
}

# ------------------------------------------------------------------ the stand-ins
STUB = r'''
import json, os, sys
w = json.load(open(os.environ["VERIFY_WORLD"]))
tool, args = os.path.basename(sys.argv[0]), sys.argv[1:]
def out(s):
    sys.stdout.write(s if s.endswith("\n") or not s else s + "\n")
if tool == "terraform":
    out(json.dumps({k: {"value": v} for k, v in w["tf"].items()}))
elif tool == "user_token.py":
    tok = w["tokens"].get(args[1], "")
    if not tok:
        print("no token for " + args[1], file=sys.stderr); sys.exit(1)
    out(tok)
elif tool in ("kubectl", "gateway_mcp_client.py"):
    if tool == "gateway_mcp_client.py":
        pod, rest = "local", args
    elif "exec" in args:
        i = args.index("exec") + 1
        if args[i] == "-i":
            i += 1
        pod, rest = args[i], args[args.index("--") + 1:]
        if rest[:2] == ["python", "-c"]:  # group 7's connect probe
            out(w["probe"].get(rest[3] + ":" + rest[4], "error: no such probe")); sys.exit()
        rest = rest[2:]  # python /client/gateway_mcp_client.py
    elif "logs" in args:
        out(w["logs"]); sys.exit()
    elif "get" in args:
        out(w["host_ip"] if "hostIP" in args[-1] else w["pod_ip"]); sys.exit()
    else:
        sys.exit(1)
    auth, url = rest[rest.index("--auth") + 1], rest[-1]
    token = sys.stdin.read().strip() if auth == "bearer" else ""
    key = "|".join((pod, auth, url, token))
    if key not in w["clients"]:
        print("Error from server (NotFound): " + key, file=sys.stderr); sys.exit(1)
    out(w["clients"][key])
elif tool == "aws":
    if args[:2] == ["iam", "simulate-principal-policy"]:
        out(w["aws_sim"].get(args[args.index("--resource-arns") + 1], ""))
    elif args[:2] == ["wafv2", "list-web-acls"]:
        out("acl-id")
    elif args[:2] == ["wafv2", "get-web-acl"]:
        out(json.dumps(w["waf_acl"]))
elif tool == "curl":
    sys.stdout.write(w["curl"])
'''

scratch = Path(tempfile.mkdtemp(prefix="verify-adv-"))
for d in ("bin", "scripts", "client", "terraform"):
    (scratch / d).mkdir()
shutil.copy(HERE / "verify.sh", scratch / "verify.sh")


def shim(path: Path, body: str, interpreter: str = "/usr/bin/env bash") -> None:
    path.write_text(f"#!{interpreter}\n" + body + "\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


# Each stand-in is the stub itself, run by this interpreter with no shell in
# between (verify.sh starts dozens of them per run). The token script and the
# local client are replaced inside the scratch copy, where verify.sh runs them
# with "$PY" (this interpreter, which has boto3 as the client's own test needs).
for name in ("terraform", "kubectl", "aws", "curl"):
    # one option word: Linux passes everything after the interpreter as a
    # single argument ("-I -S" would be one unknown option), macOS splits it
    shim(scratch / "bin" / name, STUB, interpreter=f"{sys.executable} -IS")
shim(scratch / "bin" / "sleep", "exit 0")
for name in ("terraform", "kubectl", "aws", "curl"):
    # Linux hands the rest of a shebang line to the interpreter as one argument
    first = (scratch / "bin" / name).read_text().splitlines()[0]
    assert len(first[2:].split()) <= 2, f"stand-in {name}: {first!r} has more than one option word"
for name in ("scripts/user_token.py", "client/gateway_mcp_client.py"):
    (scratch / name).write_text(STUB)


def verify(world: dict) -> tuple[int, dict[str, str], str]:
    """Run verify.sh in this world: exit code, {check name: PASS|FAIL}, output."""
    fd, path = tempfile.mkstemp(suffix=".json", dir=scratch)
    with os.fdopen(fd, "w") as f:
        json.dump(world, f)
    env = {"PATH": f"{scratch}/bin:/usr/bin:/bin", "VERIFY_WORLD": path,
           "PYTHON": sys.executable, "HOME": str(scratch), "LC_ALL": "C"}
    p = subprocess.run(["bash", str(scratch / "verify.sh")], env=env, capture_output=True, text=True, timeout=120)
    os.unlink(path)
    checks: dict[str, str] = {}
    for line in p.stdout.splitlines():
        s = line.strip()
        if s.startswith(("[PASS] ", "[FAIL] ")):
            checks.setdefault(s[7:].split(" - ")[0], s[1:5])
    return p.returncode, checks, p.stdout + p.stderr


def world(**changes) -> dict:
    """The good world with these answers replaced: clients=..., or a top-level key."""
    w = copy.deepcopy(GOOD)
    for k, v in changes.items():
        if k == "clients":
            w["clients"].update(v)
        elif k == "tf":
            w["tf"].update(v)
        else:
            w[k] = v
    return w


K = {  # the client key of each probe that group 9 and the others read
    "g1": f"static-app|sigv4|{IAM_URL}|", "g2": f"intruder|sigv4|{IAM_URL}|",
    "g3": f"ws-probe|bearer|{JWT_URL}|tok-alice", "g4": f"ws-probe|bearer|{JWT_URL}|tok-bob",
    "g4b": f"ws-probe|bearer|{JWT_URL}|tok-carol", "g5u": f"static-app|none|{IAM_URL}|",
    "g5j": f"ws-probe|none|{JWT_URL}|", "g5b": f"static-app|bearer|{IAM_URL}|tok-carol",
    "g9a": f"static-app|bearer|{JWT_URL}|tok-alice", "g9b": f"local|bearer|{JWT_URL}|tok-alice",
    "g9c": f"local|sigv4|{IAM_URL}|", "g9d": f"ws-probe|none|{IAM_URL}|",
}
REACHED_GATEWAY_EMPTY = REC["cedar_denied"]  # through the network, refused only by Cedar
SUCCEEDED = REC["app_ok"]  # a forbidden call that went through

CASES: list[tuple[str, dict, set[str]]] = []


def case(desc: str, expect_fail: set[str], **changes) -> None:
    CASES.append((desc, world(**changes), expect_fail))


# group 9a / 9d: SG_DROPPED, only a timeout
for g in ("g9a", "g9d"):
    for name, rec in (
        ("a failed name lookup (from the real client)", REC["dns"]),
        ("a TLS error (from the real client)", REC["tls"]),
        ("a refused connection (from the real client)", REC["refused"]),
        ("a successful forbidden call", SUCCEEDED),
        ("an answer from the gateway, refused by Cedar only", REACHED_GATEWAY_EMPTY),
        ("the resource policy's 403 (the network let it through)", REC["policy_refused"]),
        ("an unrelated 403", REC["unrelated_403"]),
        ("a 500", REC["server_500"]),
        ("no RESULT line at all", '{"step": "initialize", "status": 0}'),
        ("a forged timeout beside a successful call", forged(initialize=200, tools=[TOOL], call="ok", transport_error="TimeoutError: timed out")),
        ("a forged timeout with a tool list", forged(initialize=0, tools=[], transport_error="TimeoutError: timed out")),
        ("a forged timeout with the call not skipped", forged(initialize=0, call="error", transport_error="TimeoutError: timed out")),
        ("a forged timeout with initialize missing", forged(initialize=None, transport_error="TimeoutError: timed out")),
        ("a forged timeout with initialize as a string", forged(initialize="0", transport_error="TimeoutError: timed out")),
        ("a transport error that only mentions a timeout", forged(initialize=0, transport_error="URLError: <urlopen error TimeoutError>")),
        ("no transport error", forged(initialize=0)),
        ("a timeout RESULT followed by a successful one (the last RESULT counts)", REC["timeout"] + "\n" + SUCCEEDED),
        ("a timeout RESULT that does not start its line", "  " + REC["timeout"].splitlines()[-1]),
    ):
        case(f"{g}: {name}", {N[g]}, clients={K[g]: rec})

# group 9b / 9c: REFUSED, only the resource policy's 403 -32002 "Insufficient permissions"
pr = result_of(REC["policy_refused"])
for g in ("g9b", "g9c"):
    for name, rec in (
        ("a successful forbidden call", SUCCEEDED),
        ("an answer from the gateway, refused by Cedar only", REACHED_GATEWAY_EMPTY),
        ("an unrelated 403 (a WAF, a proxy)", REC["unrelated_403"]),
        ("a 403 carrying the Cedar refusal's wording", REC["cedar_wording_403"]),
        ("the refusal's body on a 401", REC["policy_401"]),
        ("a 401 for a bad token", REC["jwt_401"]),
        ("a 500", REC["server_500"]),
        ("a timeout (from the real client)", REC["timeout"]),
        ("a failed name lookup (from the real client)", REC["dns"]),
        ("a TLS error (from the real client)", REC["tls"]),
        ("a refused connection (from the real client)", REC["refused"]),
        ("no RESULT line at all", ""),
        ("a forged refusal beside a transport error", forged(**{**pr, "transport_error": "gaierror: no name"})),
        ("a forged refusal with a tool list", forged(**{**pr, "tools": []})),
        ("a forged refusal with the call denied", forged(**{**pr, "call": "denied"})),
        ("a forged refusal with the call ok", forged(**{**pr, "call": "ok", "tools": [TOOL]})),
        ("a forged refusal with the status as a string", forged(**{**pr, "initialize": "403"})),
        ("a forged refusal with -32002 but another message", forged(initialize=403, detail='{"error": {"code": -32002, "message": "Forbidden"}}')),
        ("a forged refusal with the message but no -32002", forged(initialize=403, detail='{"message": "Insufficient permissions"}')),
        ("a refusal RESULT followed by a successful one (the last RESULT counts)", REC["policy_refused"] + "\n" + SUCCEEDED),
    ):
        case(f"{g}: {name}", {N[g]}, clients={K[g]: rec})

# the other field checks
case("g1: a successful call whose tool list holds another tool too", {N["g1_ok"]},
     clients={K["g1"]: forged(initialize=200, tools=[TOOL, "hub___other"], call="ok", whoami=WHOAMI_GW)})
case("g1: a Cedar refusal where the call must succeed", {N["g1_ok"], N["g1_who"]}, clients={K["g1"]: REC["cedar_denied"]})
case("g1: a forged ok with no tool listed", {N["g1_ok"]}, clients={K["g1"]: forged(initialize=200, tools=[], call="ok", whoami=WHOAMI_GW)})
case("g1: the hub sees the calling application", {N["g1_who"]},
     clients={K["g1"]: forged(initialize=200, tools=[TOOL], call="ok", whoami={**WHOAMI_GW, "caller_arn": ARN + "static-app"})})
case("g1: the hub sees a user", {N["g1_who"]},
     clients={K["g1"]: forged(initialize=200, tools=[TOOL], call="ok", whoami={**WHOAMI_GW, "user": "alice"})})
case("g1: another actor", {N["g1_who"]},
     clients={K["g1"]: forged(initialize=200, tools=[TOOL], call="ok", whoami={**WHOAMI_GW, "actor": "static-app"})})
case("g2: the intruder's call succeeds", {N["g2_cedar"]}, clients={K["g2"]: SUCCEEDED})
case("g2: the intruder's call fails with an error, not a refusal", {N["g2_cedar"]},
     clients={K["g2"]: forged(initialize=200, tools=[], call="error", detail="-32603")})
case("g2: denied, but the tool was listed", {N["g2_cedar"]}, clients={K["g2"]: forged(initialize=200, tools=[TOOL], call="denied")})
case("g2: denied with no tool list at all", {N["g2_cedar"]}, clients={K["g2"]: forged(initialize=200, tools=None, call="denied")})
case("g2: denied, but the hub answered", {N["g2_cedar"]}, clients={K["g2"]: forged(initialize=200, tools=[], call="denied", whoami=WHOAMI_GW)})
case("g2: IAM refuses the intruder at initialize", {N["g2_init"], N["g2_cedar"]}, clients={K["g2"]: REC["unrelated_403"]})
case("g3: the hub sees another user", {N["g3_user"]},
     clients={K["g3"]: forged(initialize=200, tools=[TOOL], call="ok", whoami={**WHOAMI_GW, "user": "bob", "departments": ["team-a"]})})
case("g3: the user in no allowed team", {N["g3_user"]},
     clients={K["g3"]: forged(initialize=200, tools=[TOOL], call="ok", whoami={**WHOAMI_GW, "user": "alice", "departments": ["team-b"]})})
case("g3: a refusal where the user must get through", {N["g3_ok"], N["g3_user"]}, clients={K["g3"]: REC["cedar_denied"]})
case("g4: another team's user gets through", {N["g4_cedar"]}, clients={K["g4"]: REC["alice_ok"]})
case("g4: another team's user fails with a 500, not a refusal", {N["g4_cedar"]}, clients={K["g4"]: REC["server_500"]})
case("g4: another team's user refused by IAM at initialize, not Cedar", {N["g4_cedar"]}, clients={K["g4"]: REC["policy_refused"]})
case("g4b: the hub-denied user's call succeeds", {N["g4b_hub"]}, clients={K["g4b"]: REC["alice_ok"]})
case("g4b: refused by Cedar, not by the hub", {N["g4b_hub"]}, clients={K["g4b"]: REC["cedar_denied"]})
case("g4b: a failed call without the hub's reason", {N["g4b_hub"]},
     clients={K["g4b"]: forged(initialize=200, tools=[TOOL], call="error", detail="-32603 internal error")})
case("g4b: the hub's reason, but the tool was not listed", {N["g4b_hub"]},
     clients={K["g4b"]: forged(initialize=200, tools=[], call="error", detail="hub policy: no")})
case("g4b: the hub logged nothing", {N["g4b_log"]}, logs="")
case("g4b: the hub logged another user's refusal", {N["g4b_log"]}, logs="gateway entry refused tool hub__whoami: user=bob hub policy")
case("g5: an unsigned request gets in", {N["g5_unsigned"]}, clients={K["g5u"]: SUCCEEDED})
case("g5: an unsigned request fails with a 500", {N["g5_unsigned"]}, clients={K["g5u"]: REC["server_500"]})
case("g5: an unsigned request fails on the transport", {N["g5_unsigned"]}, clients={K["g5u"]: REC["dns"]})
case("g5: a 401 with the call not skipped", {N["g5_unsigned"]}, clients={K["g5u"]: forged(initialize=401, call="error")})
case("g5: the JWT gateway answers 401 without the discovery pointer", {N["g5_jwt401"]},
     clients={K["g5j"]: forged(initialize=401, detail='Bearer error="invalid_token"')})
case("g5: the JWT gateway answers 403 with the pointer", {N["g5_jwt401"]},
     clients={K["g5j"]: forged(initialize=403, detail='Bearer resource_metadata="x"')})
case("g5: a user's bearer token gets into the IAM gateway", {N["g5_bearer"]}, clients={K["g5b"]: SUCCEEDED})
case("g5: a user's bearer token at the IAM gateway times out", {N["g5_bearer"]}, clients={K["g5b"]: REC["timeout"]})
case("g8: the WAF allows by default", {N["g8_waf"]}, waf_acl={"WebACL": {"DefaultAction": {"Allow": {}}, "Rules": GOOD["waf_acl"]["WebACL"]["Rules"]}})
case("g8: the WAF has a second rule", {N["g8_waf"]},
     waf_acl={"WebACL": {"DefaultAction": {"Block": {}}, "Rules": GOOD["waf_acl"]["WebACL"]["Rules"] * 2}})
case("g8: the WAF's one rule blocks", {N["g8_waf"]},
     waf_acl={"WebACL": {"DefaultAction": {"Block": {}}, "Rules": [{"Action": {"Block": {}}, "Statement": {"IPSetReferenceStatement": {}}}]}})
case("g8: the WAF's one rule allows something other than an IP set", {N["g8_waf"]},
     waf_acl={"WebACL": {"DefaultAction": {"Block": {}}, "Rules": [{"Action": {"Allow": {}}, "Statement": {"GeoMatchStatement": {}}}]}})
case("g8: no WAF answer", {N["g8_waf"]}, waf_acl={})
# a few of the checks that read other answers than a RESULT
case("g6: the coder role may invoke the IAM gateway", {N["g6_iam"]}, aws_sim={"arn:gw:iam": "allowed", "arn:gw:jwt": "implicitDeny"})
case("g6: no simulation answer", {N["g6_jwt"]}, aws_sim={"arn:gw:iam": "implicitDeny"})
case("g7: a pod reaches the hub NLB", {N["g7_nlb"]}, probe={**GOOD["probe"], "hub.internal.example:443": "reached: 10.0.2.5"})
case("g7: the probe could not resolve the NLB", {N["g7_nlb"]}, probe={**GOOD["probe"], "hub.internal.example:443": "error: name did not resolve: gaierror"})
case("g7: no hub pod IP", {N["g7_pod"]}, pod_ip="")
case("g8: the ALB resolves to a public address", {N["g8_alb"]}, tf={"coder_alb_dns_name": "3.3.3.3"})
case("g8: the dashboard does not answer", {N["g8_reach"]}, curl="403")

# ------------------------------------------------------------------ run
def judge(item):
    desc, w, expect_fail = item
    rc, checks, text = verify(w)
    failed = {n for n, s in checks.items() if s == "FAIL"}
    missing = set(N.values()) - set(checks)
    good = rc != 0 and failed == expect_fail and not missing
    return desc, good, f"exit {rc}, failed {sorted(failed)}" + (f", missing {sorted(missing)}" if missing else "")


rc, checks, text = verify(GOOD)
check("the good world: verify.sh runs every check and passes them all, exit 0",
      rc == 0 and set(checks) == set(N.values()) and all(s == "PASS" for s in checks.values()) and f"{len(N)} passed, 0 failed" in text,
      f"exit {rc}, {sum(s == 'PASS' for s in checks.values())}/{len(N)} passed" + ("" if rc == 0 else "\n" + text[-1500:]))
rc, checks, _ = verify(world(tf={"hub_allowed_teams": []}))
skipped = {N["g4b_tok"], N["g4b_hub"], N["g4b_log"]}
check("with no hub rule (hub_allowed_teams []) group 4b is skipped and the rest passes",
      rc == 0 and set(checks) == set(N.values()) - skipped and all(s == "PASS" for s in checks.values()), f"exit {rc}")

with ThreadPoolExecutor(max_workers=max(4, os.cpu_count() or 4)) as pool:
    for desc, good, detail in pool.map(judge, CASES):
        check(f"{desc}: fails that check only, exit non-zero", good, "" if good else detail)

shutil.rmtree(scratch, ignore_errors=True)
print(f"\n{'FAILED: ' + ', '.join(failures) if failures else 'all passed'}")
assert not failures, f"verify.sh verdict checks failed: {failures}"
