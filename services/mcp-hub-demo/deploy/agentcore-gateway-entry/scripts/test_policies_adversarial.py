#!/usr/bin/env python3
"""The two Cedar templates of the AgentCore gateway entry, rendered as
gateway_targets.py renders them, permit exactly who they should; and the two
scripts that publish policies and fetch tokens refuse what would loosen them.

No Cedar engine here: the rendered text is parsed into its scope and its
condition, and the condition's operators (`==`, and `like` with `*` as the
only wildcard, which is all the templates use) are evaluated over claim values
as the service delivers them - every JWT claim a STRING tag, a list as its
JSON text.

  1. the JWT policy: one permit, for OAuthUser as a type (never a bare
     principal), exactly the one action, exactly this gateway (`==`); its
     condition tests hasTag first and then only the positions a list entry
     can take. Evaluated: a token whose team claim holds the allowed team,
     alone or in any position of a list, is permitted; a lookalike team, a
     single string that merely contains the quoted name, an object, a nested
     list, the team after the first entry of a list serialized with spaces,
     another case - are not (as the first entry of a spaced list it is
     still permitted: that pattern holds no comma before it). No clause
     is an unanchored substring match
  2. the IAM policy: one permit, for exactly one IamEntity by `==`, an
     assumed-role principal without a session name, never a role ARN, never
     a type test or a `like`; exactly the one action and this gateway. A
     foreign role, another account, a role whose name extends the allowed
     one, and a session-qualified ARN are different principals
  3. rendering refuses a value that would break out of a Cedar string or
     widen a pattern (a quote, a backslash, a wildcard, whitespace, a line
     break), in any of the template variables, and an unknown variable
  4. principal_of turns a role ARN into the assumed-role principal Cedar
     compares, and refuses anything that is not a role ARN (a user, an
     assumed-role ARN, a short account, a trailing line break)
  5. gateway_targets.py publishes with FAIL_ON_ANY_FINDINGS on create and on
     update, writes no policy unless both targets are READY, and reports
     success only when both policies are ACTIVE
  6. user_token.py sends a password only to a plain https issuer and follows
     no redirect

Run: python3 scripts/test_policies_adversarial.py
"""

from __future__ import annotations

import ast
import importlib.util
import io
import json
import re
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENTRY = HERE.parent
ACCOUNT = "0" * 12  # built at runtime: no 12-digit literal in a published file
GATEWAY = f"arn:aws:bedrock-agentcore:ap-northeast-1:{ACCOUNT}:gateway/mcp-gw-entry-jwt-abcde12345"
OTHER_GATEWAY = f"arn:aws:bedrock-agentcore:ap-northeast-1:{ACCOUNT}:gateway/someone-elses-xyz"
ACTION = "hub___hub__whoami"
ROLE = f"arn:aws:iam::{ACCOUNT}:role/mcp-gw-entry-static-app"
PRINCIPAL = f"arn:aws:sts::{ACCOUNT}:assumed-role/mcp-gw-entry-static-app"

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if condition else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))
    if not condition:
        failures.append(name)


def load(path: Path, name: str, stub_boto3: bool = True):
    """Import a script without its AWS client (nothing here talks to AWS)."""
    saved = sys.modules.get("boto3")
    if stub_boto3:
        sys.modules["boto3"] = type(sys)("boto3")
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        if saved is not None:
            sys.modules["boto3"] = saved
        elif stub_boto3:
            sys.modules.pop("boto3", None)


gt = load(HERE / "gateway_targets.py", "gateway_targets")


def strip_comments(text: str) -> str:
    return "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("//"))


def unescape(s: str) -> str:
    return s.replace('\\"', '"').replace("\\\\", "\\")


def cedar_like(value: str, pattern: str) -> bool:
    """Cedar's `like`: `*` matches any run of characters, everything else is literal."""
    return re.fullmatch("".join(".*" if c == "*" else re.escape(c) for c in pattern), value, re.S) is not None


def parse(policy: str) -> dict:
    """The one statement of a rendered policy: effect, scope, condition clauses."""
    body = strip_comments(policy).strip()
    statements = [s for s in body.split(";") if s.strip()]
    assert len(statements) == 1, f"expected one statement, found {len(statements)}"
    st = statements[0]
    m = re.match(r"\s*(permit|forbid)\s*\((.*?)\)\s*(?:when\s*\{(.*)\})?\s*$", st, re.S)
    assert m, f"not a permit/forbid statement: {st[:120]}"
    scope = [p.strip() for p in m.group(2).split(",\n")]
    cond = (m.group(3) or "").strip()
    return {"effect": m.group(1), "scope": scope, "cond": cond}


# ------------------------------------------------------------------ 1. the JWT policy
def render_jwt(team: str = "team-a", claim: str = "team", gateway: str = GATEWAY) -> str:
    return gt.render("jwt.cedar.tpl", {"ACTION": ACTION, "GATEWAY_ARN": gateway, "TEAM_CLAIM": claim, "ALLOWED_TEAM": team})


jwt = parse(render_jwt())
check("JWT policy: a single permit (no forbid, no second statement)", jwt["effect"] == "permit")
check("JWT policy: the principal is the OAuthUser type, the action exactly the one tool, the resource exactly this gateway",
      jwt["scope"] == ["principal is AgentCore::OAuthUser", f'action == AgentCore::Action::"{ACTION}"', f'resource == AgentCore::Gateway::"{GATEWAY}"'],
      str(jwt["scope"]))
check("JWT policy: rendering for another gateway changes the resource (the template hard-codes none)",
      parse(render_jwt(gateway=OTHER_GATEWAY))["scope"][2] == f'resource == AgentCore::Gateway::"{OTHER_GATEWAY}"')
cond = jwt["cond"]
m = re.fullmatch(r'principal\.hasTag\("team"\)\s*&&\s*\((.*)\)', cond, re.S)
check("JWT policy: the condition is hasTag AND one parenthesized group of alternatives (a missing claim never reaches getTag)", m is not None, cond[:160])
clauses = [c.strip() for c in re.split(r"\|\|", m.group(1))] if m else []
parsed = []
for c in clauses:
    cm = re.fullmatch(r'principal\.getTag\("team"\)\s+(==|like)\s+"((?:[^"\\]|\\.)*)"', c)
    check(f"JWT policy: clause reads the team claim with == or like only: {c[:70]}", cm is not None)
    if cm:
        parsed.append((cm.group(1), unescape(cm.group(2))))
check("JWT policy: the alternatives are exactly the five positions (only entry, first, last, between, bare string)",
      sorted(parsed) == sorted([("==", '["team-a"]'), ("like", '["team-a",*]'), ("like", '[*,"team-a"]'), ("like", '[*,"team-a",*]'), ("==", "team-a")]),
      str(parsed))
for op, pat in parsed:
    if op == "like":
        check(f"JWT policy: `like` {pat!r} is anchored on the list brackets at both ends (no unanchored substring match)",
              pat.startswith("[") and pat.endswith("]") and not pat.startswith("*") and not pat.endswith("*"))


def jwt_permits(tag: str | None) -> bool:
    if tag is None:  # no such claim: hasTag is false
        return False
    return any((tag == pat) if op == "==" else cedar_like(tag, pat) for op, pat in parsed)


J = lambda v: json.dumps(v, separators=(",", ":"))  # noqa: E731 - how an IdP serializes a list claim
for tag in (J(["team-a"]), J(["team-a", "team-b"]), J(["team-b", "team-a"]), J(["team-b", "team-a", "team-c"]),
            J(["a", "b", "c", "team-a"]), J(["team-a", "team-a"]), "team-a"):
    check(f"JWT policy permits a member of the allowed team: {tag}", jwt_permits(tag))
# a spaced list: the first-entry pattern still holds (no comma precedes the team)
check("JWT policy permits the allowed team as the first entry of a list serialized with spaces",
      jwt_permits(json.dumps(["team-a", "team-b"])))
for why, tag in (
    ("another team", J(["team-b"])), ("no team", J([])), ("no claim at all", None), ("an empty string", ""),
    ("a team whose name extends the allowed one", J(["team-ab"])), ("... prefixed", J(["x-team-a"])), ("... suffixed with a role", J(["team-a-admins"])),
    ("lookalikes in a list", J(["team-b", "team-ab"])), ("a glued prefix in first position", J(["ateam-a", "team-b"])),
    ("a bare lookalike", "team-ab"), ("another case", "TEAM-A"), ("another case in a list", J(["Team-A"])),
    ("a trailing space", J(["team-a "])), ("a leading space", " team-a"),
    # the judge's case: a single string that merely contains the quoted name
    ("a single string containing the quoted name", 'other "team-a" group'),
    ("the quoted name alone", '"team-a"'), ("the quoted name between commas", ',"team-a",'),
    ("an unterminated list", '["team-a"'), ("a list with no opening bracket", '"team-a"]'),
    ("a comma form with no brackets", 'x,"team-a",y'), ("a half-open first-position form", '["team-a",x'), ("a half-open last-position form", 'x,"team-a"]'),
    ("an object with the team as a key", J({"team-a": True})), ("an object with the team as a value", J({"name": "team-a"})),
    ("an object with the team between other members", J({"a": 1, "b": "team-a", "c": 2})),
    ("a nested list", J([["team-a"]])),
    ("a list serialized with spaces, the team last (denied: the closed direction)", json.dumps(["team-b", "team-a"])),
    ("a list serialized with spaces, the team between (denied: the closed direction)", json.dumps(["team-b", "team-a", "team-c"])),
    ("a lookalike first in a list serialized with spaces", json.dumps(["team-ab", "team-b"])),
    ("the JSON null", "null"), ("the JSON true", "true"),
):
    check(f"JWT policy denies {why}: {tag!r}", not jwt_permits(tag))
other = parse(render_jwt(team="team-b"))["cond"]
check("JWT policy: rendering for another team names that team and not this one", '"team-b' in other.replace('\\"', '"') and "team-a" not in other)

# ------------------------------------------------------------------ 2. the IAM policy
iam = parse(gt.render("iam.cedar.tpl", {"ACTION": ACTION, "GATEWAY_ARN": GATEWAY, "PRINCIPAL": PRINCIPAL}))
check("IAM policy: a single unconditional permit", iam["effect"] == "permit" and iam["cond"] == "", iam["cond"][:80])
check("IAM policy: exactly one IamEntity by ==, exactly the one action, exactly this gateway",
      iam["scope"] == [f'principal == AgentCore::IamEntity::"{PRINCIPAL}"', f'action == AgentCore::Action::"{ACTION}"', f'resource == AgentCore::Gateway::"{GATEWAY}"'],
      str(iam["scope"]))
raw_iam = strip_comments((ENTRY / "policies" / "iam.cedar.tpl").read_text())
check("IAM policy: no type test, no like, no bare principal, no when (nothing that could match a second role)",
      "principal is" not in raw_iam and " like " not in raw_iam and "when" not in raw_iam and not re.search(r"\(\s*principal\s*,", raw_iam))
permitted = re.search(r'IamEntity::"([^"]+)"', iam["scope"][0]).group(1)
check("IAM policy: the principal is an assumed-role ARN without a session name (what the service compares)",
      re.fullmatch(r"arn:aws:sts::\d{12}:assumed-role/[\w+=,.@-]+", permitted) is not None, permitted)
for why, other_principal in (
    ("the intruder's role", f"arn:aws:sts::{ACCOUNT}:assumed-role/mcp-gw-entry-intruder"),
    ("the coder's role", f"arn:aws:sts::{ACCOUNT}:assumed-role/mcp-gw-entry-coder"),
    ("the same role name in another account", f"arn:aws:sts::{'1' * 12}:assumed-role/mcp-gw-entry-static-app"),
    ("a role whose name extends the allowed one", f"arn:aws:sts::{ACCOUNT}:assumed-role/mcp-gw-entry-static-app-admin"),
    ("the role ARN itself (iam, not sts)", ROLE),
    ("a session-qualified ARN", PRINCIPAL + "/botocore-session-1"),
    ("an IAM user", f"arn:aws:iam::{ACCOUNT}:user/static-app"),
):
    check(f"IAM policy: {why} is a different principal", other_principal != permitted)

# ------------------------------------------------------------------ 3. rendering refuses what would widen a policy
def refuses(fn) -> bool:
    try:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            fn()
    except SystemExit:
        return True
    except Exception:  # noqa: BLE001 - a KeyError is a refusal of an unknown variable too
        return True
    return False


BAD = {"a double quote": 'a"b', "a quote closing the string early": 'team-a" || true || "', "a backslash": "a\\b", "a wildcard": "team-*",
       "a question mark": "team-?", "a space": "team a", "a tab": "team\ta", "a line break": "team-a\nteam-b", "a carriage return": "a\rb", "empty": ""}
for label, value in BAD.items():
    check(f"render refuses {label} as the allowed team", refuses(lambda v=value: render_jwt(team=v)))
    check(f"render refuses {label} as the team claim name", refuses(lambda v=value: render_jwt(claim=v)))
    check(f"render refuses {label} as the gateway ARN", refuses(lambda v=value: render_jwt(gateway=v)))
    check(f"render refuses {label} as the IAM principal",
          refuses(lambda v=value: gt.render("iam.cedar.tpl", {"ACTION": ACTION, "GATEWAY_ARN": GATEWAY, "PRINCIPAL": v})))
check("render refuses a template variable it was not given", refuses(lambda: gt.render("jwt.cedar.tpl", {"ACTION": ACTION, "GATEWAY_ARN": GATEWAY, "TEAM_CLAIM": "team"})))
check("the templates use only the variables gateway_targets passes",
      set(re.findall(r"\$\{([A-Z_]+)\}", (ENTRY / "policies" / "jwt.cedar.tpl").read_text())) == {"ACTION", "GATEWAY_ARN", "TEAM_CLAIM", "ALLOWED_TEAM"}
      and set(re.findall(r"\$\{([A-Z_]+)\}", (ENTRY / "policies" / "iam.cedar.tpl").read_text())) == {"ACTION", "GATEWAY_ARN", "PRINCIPAL"})

# ------------------------------------------------------------------ 4. principal_of
check("principal_of: a role ARN becomes the assumed-role principal", gt.principal_of(ROLE) == PRINCIPAL, gt.principal_of(ROLE))
check("principal_of: a role with a path keeps its name only (an assumed-role ARN carries no path)",
      gt.principal_of(f"arn:aws:iam::{ACCOUNT}:role/team/x/mcp-gw-entry-static-app") == PRINCIPAL)
for why, arn in (("an IAM user", f"arn:aws:iam::{ACCOUNT}:user/x"), ("an assumed-role ARN", PRINCIPAL), ("a short account id", "arn:aws:iam::123:role/x"),
                 ("a wildcard role", f"arn:aws:iam::{ACCOUNT}:role/*"), ("a trailing line break", ROLE + "\n"), ("text after the name", ROLE + " x"),
                 ("an empty string", ""), ("a role in a path only", f"arn:aws:iam::{ACCOUNT}:role/path/")):
    check(f"principal_of refuses {why}", refuses(lambda a=arn: gt.principal_of(a)))

# ------------------------------------------------------------------ 4b. one permit per allowed team
check("a team's policy is named after it (letters, digits, underscores)", gt.team_policy_name("team-c") == "hub_whoami_by_team_team_c")
check("two teams get two policies", gt.team_policy_names(["team-a", "team-c"]) == {"team-a": "hub_whoami_by_team_team_a", "team-c": "hub_whoami_by_team_team_c"})
check("two teams that would share a policy name are refused (team-a, team_a)", refuses(lambda: gt.team_policy_names(["team-a", "team_a"])))
check("the same team twice is refused", refuses(lambda: gt.team_policy_names(["team-a", "team-a"])))
for why, team in (("a quote", 'team"a'), ("a space", "team a"), ("an empty name", ""), ("65 characters", "a" * 65)):
    check(f"team_policy_name refuses {why}", refuses(lambda t=team: gt.team_policy_name(t)))
existing = {"hub_whoami_by_team": "p0", "hub_whoami_by_team_team_a": "p1", "hub_whoami_by_team_team_c": "p2", "hub_whoami_by_role": "p3", "other": "p4"}
check("stale team policies: the old single policy and teams no longer allowed, nothing else",
      gt.stale_team_policies(existing, {"hub_whoami_by_team_team_a"}) == ["hub_whoami_by_team", "hub_whoami_by_team_team_c"],
      str(gt.stale_team_policies(existing, {"hub_whoami_by_team_team_a"})))
check("stale team policies never include a wanted one or another engine's role policy",
      gt.stale_team_policies(existing, {"hub_whoami_by_team_team_a", "hub_whoami_by_team_team_c"}) == ["hub_whoami_by_team"])
for team, other in (("team-a", "team-c"), ("team-c", "team-a")):
    body = strip_comments(render_jwt(team=team))  # the template's comments use team-a as their example
    check(f"{team}'s rendered permit names {team} and not {other}", f'\\"{team}\\"' in body and other not in body)

# ------------------------------------------------------------------ 5. how gateway_targets publishes
src = (HERE / "gateway_targets.py").read_text()
tree = ast.parse(src)
calls = {}
for node in ast.walk(tree):
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ("create_policy", "update_policy"):
        calls[node.func.attr] = {k.arg: ast.unparse(k.value) for k in node.keywords}
for op in ("create_policy", "update_policy"):
    check(f"{op} publishes with FAIL_ON_ANY_FINDINGS (an update is validated as strictly as a create)",
          calls.get(op, {}).get("validationMode") == "'FAIL_ON_ANY_FINDINGS'", str(calls.get(op)))
check("update_policy wraps the description as the API requires ({'optionalValue': ...}); a bare string is rejected by the SDK before any call",
      calls.get("update_policy", {}).get("description") == "{'optionalValue': description}", str(calls.get("update_policy", {}).get("description")))
main_src = ast.unparse(next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "main"))
order = (main_src.find("ensure_target("), main_src.find("if not ok:"), main_src.find("return 1"), main_src.find("ensure_policy("))
check("no policy is written unless both targets are READY (the action exists only once the target does)",
      -1 < order[0] < order[1] < order[2] < order[3], str(order))
check("the run succeeds only when both policies are ACTIVE", main_src.rstrip().endswith("return 0 if ok else 1") and main_src.count("ensure_policy(") == 2)
ep = ast.unparse(next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "ensure_policy"))
check("ensure_policy reports success only for ACTIVE", "return state['status'] == 'ACTIVE'" in ep)
et = ast.unparse(next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "ensure_target"))
check("ensure_target reports success only for READY, and always sets the private endpoint and the CA certificate",
      "return state['status'] == 'READY'" in et and "'privateEndpoint'" in et and "'certificateConfigurations'" in et)

# ------------------------------------------------------------------ 6. user_token.py: where a password may go
ut_src = (HERE / "user_token.py").read_text()
ut = ast.parse(ut_src)
check("user_token.py opens its request through an opener with its own redirect handler, never urlopen",
      "urlopen(" not in ut_src and "build_opener(_NoRedirect" in ut_src)
nr = next((n for n in ast.walk(ut) if isinstance(n, ast.ClassDef) and n.name == "_NoRedirect"), None)
check("... whose redirect_request returns None for every redirect",
      nr is not None and "HTTPRedirectHandler" in ast.unparse(nr.bases[0])
      and ast.unparse(next(f for f in nr.body if isinstance(f, ast.FunctionDef) and f.name == "redirect_request").body[-1]) == "return None")
guard = re.search(r'if parts\.scheme != "https" or not parts\.hostname or parts\.username or parts\.password or parts\.query or parts\.fragment:\s*\n\s*print\([^\n]*\n\s*return 1', ut_src)
check("the issuer must be a plain https URL (no http, no userinfo, no query, no fragment) before anything is sent", guard is not None)
check("... and that check comes before the request is built",
      guard is not None and ut_src.find("build_opener(") > guard.start() and ut_src.find('"/protocol/openid-connect/token"') > guard.start())
# what user_token.py prints: verify.sh reads the token from stdout, so stdout
# carries the token and nothing else, and no print names a password (CodeQL
# py/clear-text-logging-sensitive-data on the published copy, 2026-11-23)
prints = [n for n in ast.walk(ut) if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "print"]
to_stdout = [ast.unparse(n) for n in prints if not any(k.arg == "file" for k in n.keywords)]
check("user_token.py prints one thing on stdout: the access token of the token endpoint's answer",
      to_stdout == ["print(json.load(r)['access_token'])"], str(to_stdout))
named = {ast.unparse(n): {x.id for a in n.args for x in ast.walk(a) if isinstance(x, ast.Name)} for n in prints}
check("... and no print takes the demo users' passwords, the request body or the Secrets Manager answer",
      not [c for c, ids in named.items() if ids & {"users", "data", "sm"}], str(named))
import urllib.parse  # noqa: E402


def issuer_ok(issuer: str) -> bool:  # the guard's own expression, evaluated
    parts = urllib.parse.urlsplit(issuer)
    return not (parts.scheme != "https" or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment)


check("a plain https issuer is accepted", issuer_ok("https://idp.example.com/realms/agent-platform"))
for why, issuer in (("plain http", "http://idp.example.com/realms/x"), ("userinfo", "https://user:pw@idp.example.com/realms/x"),
                    ("userinfo hiding the real host", "https://idp.example.com@evil.example/realms/x"), ("a query", "https://idp.example.com/realms/x?next=//evil"),
                    ("a fragment", "https://idp.example.com/realms/x#frag"), ("no host", "https:///realms/x"), ("another scheme", "file:///etc/passwd"),
                    ("no scheme", "idp.example.com/realms/x")):
    check(f"an issuer with {why} is refused", not issuer_ok(issuer))

print(f"\n{'FAILED: ' + ', '.join(failures) if failures else 'all passed'}")
assert not failures, f"policy checks failed: {failures}"
