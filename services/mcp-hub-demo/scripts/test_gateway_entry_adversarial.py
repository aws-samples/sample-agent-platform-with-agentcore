"""Unit test for hub/gateway_entry.py: what the hub does behind an AgentCore gateway.

With ``gateway_entry: {trust: network}`` a request that carries no credentials
is believed to be an application the gateway admitted. That is the one path
into the hub without a verified token, so the test is about what must NOT be
able to take it: an empty or malformed credential, another scheme, a header
that names an application on one of the hub's other paths. Each must be
refused, never downgraded to an application-only call.

It also pins the wiring that makes the decision count: app.py asks this module
before any other branch and for nothing else, and every place that ships the
hub's source ships this file (a hub started without it does not start).

No IdP, no AWS, standard library only. Run:
    python scripts/test_gateway_entry_adversarial.py
"""

from __future__ import annotations

import ast
import importlib.util
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
HUB = HERE / "hub"
spec = importlib.util.spec_from_file_location("gateway_entry", HUB / "gateway_entry.py")
ge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ge)

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if condition else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))
    if not condition:
        failures.append(name)


def refuses(cfg, has_entry_identity: bool = False) -> bool:
    try:
        ge.load_gateway_entry(cfg, has_entry_identity)
    except SystemExit:
        return True
    return False


GOOD = {"trust": "network", "actor": "agentcore-gateway"}
APP = {"actor": "agentcore-gateway", "trust": "network", "via": "agentcore-gateway", "caller_arn": None}

# ---------------------------------------------------------------- the block
entry = ge.load_gateway_entry(GOOD, False)
check("a valid block loads", entry == {"actor": "agentcore-gateway", "trust": "network", "via": "agentcore-gateway"}, str(entry))
for name, cfg in (("no block", None), ("an empty block", {}), ("an empty string", "")):
    check(f"{name}: the entry is off", ge.load_gateway_entry(cfg, False) is None)
check("off stays off beside an entry_identity block", ge.load_gateway_entry(None, True) is None)
for name, cfg in (
    ("trust: secret", {"trust": "secret", "actor": "a"}),
    ("trust missing", {"actor": "a"}),
    ("trust: Network (case)", {"trust": "Network", "actor": "a"}),
    ("trust as a list", {"trust": ["network"], "actor": "a"}),
    ("trust: true", {"trust": True, "actor": "a"}),
    ("actor missing", {"trust": "network"}),
    ("actor empty", {"trust": "network", "actor": ""}),
    ("actor None", {"trust": "network", "actor": None}),
    ("actor with a space", {"trust": "network", "actor": "a b"}),
    ("actor with a slash", {"trust": "network", "actor": "a/b"}),
    ("actor with a colon (an ARN)", {"trust": "network", "actor": "arn:aws:iam::x:role/y"}),
    ("actor starting with a dot", {"trust": "network", "actor": ".a"}),
    ("actor of 65 characters", {"trust": "network", "actor": "a" * 65}),
    ("actor with a trailing newline", {"trust": "network", "actor": "a\n"}),
    ("actor with a line break inside", {"trust": "network", "actor": "a\nb"}),
    ("actor as a number", {"trust": "network", "actor": 7}),
    ("actor as a list", {"trust": "network", "actor": ["a"]}),
    ("the block as a list", ["network"]),
    ("the block as a string", "network"),
    ("the block as True", True),
):
    check(f"refused at load: {name}", refuses(cfg))
check("refused at load: a valid block beside entry_identity (one listener, one front)", refuses(GOOD, True))
check("an actor of 64 characters loads", ge.load_gateway_entry({"trust": "network", "actor": "a" * 64}, False) is not None)

# ---------------------------------------------------------------- the hub's own rule on users
LIMITED = ge.load_gateway_entry({**GOOD, "allowed_departments": ["team-a", "team-a"]}, False)
check("allowed_departments loads, deduplicated", LIMITED.get("allowed_departments") == ["team-a"], str(LIMITED))
check("without allowed_departments the entry carries no such key (no rule of the hub's own)", "allowed_departments" not in entry)
for name, value in (
    ("an empty list (would refuse every user while looking like a rule)", []),
    ("a string, not a list", "team-a"),
    ("None", None),
    ("a number inside", ["team-a", 7]),
    ("an empty name", [""]),
    ("a name with a space", ["team a"]),
    ("a name with a quote", ['team"a']),
    ("a name with a line break", ["team-a\nx"]),
    ("a name of 65 characters", ["a" * 65]),
    ("a dict", {"team-a": True}),
):
    check(f"refused at load: allowed_departments as {name}", refuses({**GOOD, "allowed_departments": value}))
check("refused at load: an unknown key beside trust and actor (a typo must not silently drop a rule)",
      refuses({**GOOD, "allowed_department": ["team-a"]}))

for name, depts, has_user, ok in (
    ("a user in the allowed department (alice)", {"team-a"}, True, True),
    ("a user in several departments, one allowed", {"team-c", "team-a"}, True, True),
    ("a user the gateway admitted from another department (carol)", {"team-c"}, True, False),
    ("a user with no department at all", set(), True, False),
    ("a department differing only in case", {"Team-A"}, True, False),
    ("a department that only starts like the allowed one", {"team-a-ext"}, True, False),
    ("no user: the application alone is not judged here", set(), False, True),
):
    reason = ge.user_may_call(LIMITED, depts, has_user)
    check(f"with allowed_departments: {name} -> {'runs' if ok else 'refused'}", (reason is None) == ok, str(reason))
check("without allowed_departments any admitted user runs (the gateway's policy is the only one)",
      ge.user_may_call(entry, {"team-c"}, True) is None)
reason, token, app = ge.gateway_request({}, LIMITED)
check("the application the hub attributes a call to carries its identity, not its rule", app == APP, str(app))
check("a refusal says it is the hub's and names the departments",
      (ge.user_may_call(LIMITED, {"team-c"}, True) or "").startswith("hub policy: departments ['team-c']"))

# ---------------------------------------------------------------- one request
reason, token, app = ge.gateway_request({}, entry)
check("no credentials at all: the application alone", reason is None and token == "" and app == APP, str((reason, token, app)))
reason, token, app = ge.gateway_request({"authorization": "Bearer abc.def.ghi"}, entry)
check("a Bearer: the token to verify, beside the application", reason is None and token == "abc.def.ghi" and app == APP, str((reason, token)))
reason, token, app = ge.gateway_request({"authorization": "Bearer abc.def.ghi", "content-type": "application/json", "x-amzn-trace-id": "Root=1"}, entry)
check("unrelated headers change nothing", reason is None and token == "abc.def.ghi" and app == APP)
check("the returned application is a copy: a caller cannot alter the entry through it",
      app is not entry and "caller_arn" not in entry)

for name, headers in (
    ("an empty Authorization header (present, not absent)", {"authorization": ""}),
    ("Bearer with nothing after it", {"authorization": "Bearer "}),
    ("Bearer followed by spaces only", {"authorization": "Bearer    "}),
    ("Bearer followed by a tab", {"authorization": "Bearer \t"}),
    ("'Bearer' without the space", {"authorization": "Bearer"}),
    ("lower-case scheme", {"authorization": "bearer abc"}),
    ("Basic", {"authorization": "Basic dXNlcjpwYXNz"}),
    ("the HMAC scheme of the hub's other path", {"authorization": "MCPHUB-HMAC-SHA256 Credential=x"}),
    ("a SigV4 header (the gateway does not forward one)", {"authorization": "AWS4-HMAC-SHA256 Credential=AKID/20260101/r/s/aws4_request"}),
    ("a leading space before Bearer", {"authorization": " Bearer abc"}),
    ("the IAM entry's identity header", {"x-caller-arn": "arn:aws:sts::" + "0" * 12 + ":assumed-role/r/s"}),
    ("the IAM entry's identity header, empty", {"x-caller-arn": ""}),
    ("the IAM entry's secret header", {"x-mcp-hub-entry-secret": "s3cret"}),
    ("the HMAC path's user-token header", {"x-mcphub-sso-token": "abc.def.ghi"}),
    ("an identity header next to a valid Bearer", {"x-caller-arn": "arn:x", "authorization": "Bearer abc.def.ghi"}),
    ("a secret header next to a valid Bearer", {"x-mcp-hub-entry-secret": "s", "authorization": "Bearer abc.def.ghi"}),
):
    reason, token, app = ge.gateway_request(headers, entry)
    check(f"refused: {name}", bool(reason) and token is None and app is None, str((reason, token, app)))

check("the three application headers of the other paths are the refused set",
      set(ge.REFUSED_HEADERS) == {"x-caller-arn", "x-mcp-hub-entry-secret", "x-mcphub-sso-token"}, str(ge.REFUSED_HEADERS))

# ---------------------------------------------------------------- the wiring
app_src = (HUB / "app.py").read_text()
tree = ast.parse(app_src)
imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module == "gateway_entry" for a in n.names}
check("app.py imports the three decisions from gateway_entry", imported == {"gateway_request", "load_gateway_entry", "user_may_call"}, str(imported))
call_tool = next((n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "on_call_tool"), None)
first = call_tool.body[0] if call_tool is not None and call_tool.body else None
check("on_call_tool asks user_may_call first, before any tool runs (whoami included)",
      isinstance(first, ast.If) and "GATEWAY" in ast.unparse(first.test) and "user_may_call(GATEWAY" in ast.unparse(first)
      and any(isinstance(n, ast.Return) for n in ast.walk(first)), ast.unparse(first)[:120] if first else "none")
check("app.py loads the entry from the gateway_entry block and tells it whether entry_identity is set",
      bool(re.search(r'GATEWAY\s*=\s*load_gateway_entry\(CONFIG\.get\("gateway_entry"\),\s*bool\(CONFIG\.get\("entry_identity"\)\)\)', app_src)))
call = next((n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "__call__"), None)
branches = []
if call is not None:
    first_if = next((n for n in call.body if isinstance(n, ast.If) and "GATEWAY" in ast.unparse(n.test)), None)
    node = first_if
    while isinstance(node, ast.If):
        branches.append(ast.unparse(node.test))
        node = node.orelse[0] if len(node.orelse) == 1 and isinstance(node.orelse[0], ast.If) else None
check("the gateway branch is judged first: with the entry on, no other branch (IAM entry, HMAC, bare Bearer) is reachable",
      bool(branches) and branches[0] == "GATEWAY is not None", str(branches[:2]))
if call is not None and first_if is not None:
    body_src = "\n".join(ast.unparse(n) for n in first_if.body)
    check("that branch takes its whole answer from gateway_request and refuses on a reason",
          "reason, raw, app_identity = gateway_request(headers, GATEWAY)" in body_src
          and re.search(r"if reason:\s*\n\s*log\.warning\('gateway entry refused: %s', reason\)\s*\n\s*await self\._unauthorized\(send, reason\)\s*\n\s*return", body_src) is not None
          and len(first_if.body) == 2, body_src[:260])
else:
    check("that branch takes its whole answer from gateway_request and refuses on a reason", False, "no gateway branch found")
verify_guard = re.search(r"if raw or app_identity is None:\s*\n\s*try:\s*\n\s*claims = await anyio\.to_thread\.run_sync\(self\._verify, raw\)", app_src)
check("a token that is present is always verified; only an application-only call goes without one", verify_guard is not None)

# the audit record: this is the one path in without a credential
admitted = re.search(r"if GATEWAY is not None:\s*\n(?:\s*#[^\n]*\n)*\s*user = [^\n]+\n\s*log\.info\(\"gateway entry admitted: actor=%s user=%s\", app_identity\[\"actor\"\], user if claims else \"none \(application only\)\"\)", app_src)
check("an admitted call leaves a record naming the actor and the user, or that there is none", admitted is not None)
order = (app_src.find("claims = await anyio.to_thread.run_sync(self._verify, raw)"), app_src.find('log.info("gateway entry admitted'), app_src.find("t3 = current_app.set(app_identity)"))
check("... written after the token verified and before the request is handed on (a rejected token is never logged as admitted)",
      -1 < order[0] < order[1] < order[2], str(order))
check("no log line on this path can carry a header value (only the reason, the configured actor and verified claims)",
      "headers[" not in app_src[app_src.find("if GATEWAY is not None:"):app_src.find("elif ENTRY is not None")]
      and all("authorization" not in line.lower() or "log." not in line for line in app_src.splitlines()))

# every module app.py imports from its own directory must be shipped wherever the hub's source is
local_modules = sorted({n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module and (HUB / f"{n.module}.py").exists()})
check("app.py's local modules are the three expected ones", local_modules == ["entry_identity", "gateway_entry", "mcphub_hmac"], str(local_modules))
shippers = {
    "hub/Dockerfile": (HUB / "Dockerfile").read_text(),
    "deploy/eks-iam-entry/deploy.sh": (HERE / "deploy" / "eks-iam-entry" / "deploy.sh").read_text(),
    "deploy/agentcore-gateway-entry/deploy.sh": (HERE / "deploy" / "agentcore-gateway-entry" / "deploy.sh").read_text(),
}
for where, text in shippers.items():
    missing = [m for m in local_modules if f"{m}.py" not in text]
    check(f"{where} ships every local module app.py imports (a hub missing one does not start)", not missing, f"missing {missing}")

print(f"\n{'FAILED: ' + ', '.join(failures) if failures else 'all passed'}")
assert not failures, f"gateway entry checks failed: {failures}"
