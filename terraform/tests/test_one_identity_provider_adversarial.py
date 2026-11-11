#!/usr/bin/env python3
"""Adversarial check on the foundation: one identity provider per deployment.

An OIDC backend that also carries a Cognito pool accepts the pool's ID tokens
when the realm does not verify one (the application's
backend/app/dependencies.py chains to the pool), so a pool next to an issuer
is a second way in: on the 533 deployment its admin password reached the API
beside the Keycloak realm. The schedule-runner no longer signs in to the
portal (private service entry since 2026-11-01), so nothing needs the pool
there either. Static, no credentials.

  1. the foundation declares exactly three Cognito resources (the pool, its
     app client, the admin group), all in modules/portal and each under
     count = var.enable_cognito ? 1 : 0; no other aws_cognito_* anywhere
  2. the root sets enable_cognito = local.oidc_issuer == "", the same
     local the facts publish as portal.oidc.issuer (ssm.tf), so the pool
     exists exactly when the backend has no issuer
  3. moved blocks carry the pool, client and group to their [0] addresses:
     a deployment that keeps Cognito keeps its pool and its users, it does
     not replace them
  4. the facts publish the pool and client ids as try(...[0].id, ""),
     never null (the workloads' type takes "" for "no pool"), and nothing of
     the portal-admin secret: no output, no fact, no local
  5. the schedule-runner's role grants no Secrets Manager read: its only
     way to the backend is the service entry, signed with its own role
  6. the checker itself rejects each evasion: run on hostile edits of the
     real files (a gate dropped, the gate inverted or tied to something else,
     an ungated pool elsewhere, a moved block dropped, a null fact, the admin
     secret back as a fact or as a runner grant), every one must fail

Run: python3 terraform/tests/test_one_identity_provider_adversarial.py
"""

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TF = os.path.join(ROOT, "terraform")
PORTAL = "modules/portal/"
COGNITO = {
    ("aws_cognito_user_pool", "portal"),
    ("aws_cognito_user_pool_client", "portal"),
    ("aws_cognito_user_group", "admin"),
}
GATE = re.compile(r"^\s*count\s*=\s*var\.enable_cognito\s*\?\s*1\s*:\s*0\s*$", re.M)


def ok(msg: str) -> None:
    print(f"  ok   {msg}")


def strip(text: str) -> str:
    return "\n".join(line.split("#", 1)[0] for line in text.splitlines())


def block(text: str, header: str) -> str:
    start = text.index(header)
    depth = 0
    for i in range(text.index("{", start), len(text)):
        depth += {"{": 1, "}": -1}.get(text[i], 0)
        if depth == 0:
            return text[start:i + 1]
    raise AssertionError(f"unterminated block {header}")


def load() -> dict:
    files = {}
    for base, _dirs, names in os.walk(TF):
        rel = os.path.relpath(base, TF)
        if rel.split(os.sep)[0] == "tests" or any(part.startswith(".") for part in rel.split(os.sep) if part != "."):
            continue
        for n in names:
            if n.endswith(".tf"):
                p = os.path.join(base, n)
                with open(p, encoding="utf-8") as f:
                    files[os.path.relpath(p, TF)] = f.read()
    return files


def check(files: dict, report=ok) -> None:
    code = {p: strip(t) for p, t in files.items()}
    # 1
    found = {}
    for p, t in code.items():
        for rtype, name in re.findall(r'^resource\s+"(aws_cognito_[a-z_]+)"\s+"([^"]+)"', t, re.M):
            found[(rtype, name)] = p
    assert set(found) == COGNITO, f"Cognito resources: {sorted(found)}"
    for (rtype, name), p in found.items():
        assert p.startswith(PORTAL), f"{rtype}.{name} outside modules/portal ({p})"
        body = block(code[p], f'resource "{rtype}" "{name}"')
        assert GATE.search(body), f"{rtype}.{name} is not under count = var.enable_cognito ? 1 : 0"
    report("the pool, client and group exist only under enable_cognito; no other Cognito resource")

    # 2
    root = code["main.tf"]
    wiring = block(root, 'module "portal"')
    m = re.findall(r"^\s*enable_cognito\s*=\s*(.+?)\s*$", wiring, re.M)
    assert m == ['local.oidc_issuer == ""'], f"enable_cognito wiring: {m}"
    ssm = code["ssm.tf"]
    oidc = block(ssm, "oidc = {")
    assert re.search(r"^\s*issuer\s*=\s*local\.oidc_issuer\s*$", oidc, re.M), "the facts' issuer is not local.oidc_issuer"
    report('enable_cognito = local.oidc_issuer == "", the issuer the facts publish')

    # 3
    portal_main = code[PORTAL + "main.tf"]
    for rtype, name in COGNITO:
        assert re.search(
            rf"moved\s*\{{\s*from\s*=\s*{rtype}\.{name}\s+to\s*=\s*{rtype}\.{name}\[0\]\s*\}}", portal_main
        ), f"no moved block {rtype}.{name} -> [0]"
    report("moved blocks keep an existing pool, client and group in place")

    # 4
    facts = block(code[PORTAL + "outputs.tf"], 'output "workload_facts"')
    assert re.search(r'cognito_pool_id\s*=\s*try\(aws_cognito_user_pool\.portal\[0\]\.id,\s*""\)', facts), "cognito_pool_id fact"
    assert re.search(r'cognito_client_id\s*=\s*try\(aws_cognito_user_pool_client\.portal\[0\]\.id,\s*""\)', facts), "cognito_client_id fact"
    for p, t in code.items():
        assert "portal_admin_secret" not in t and "portal-admin" not in t, f"the portal-admin secret is still named in {p}"
    report('the facts publish "" for no pool and nothing of the portal-admin secret')

    # 5
    runner = block(code[PORTAL + "scheduler.tf"], 'data "aws_iam_policy_document" "schedule_runner"')
    assert "secretsmanager:" not in runner, "the schedule-runner's role reads a secret"
    report("the schedule-runner's role grants no Secrets Manager read")


def mutants(files: dict) -> dict:
    out = {}
    pm, po, ps = PORTAL + "main.tf", PORTAL + "outputs.tf", PORTAL + "scheduler.tf"

    def edit(name, path, old, new, count=1):
        assert old in files[path], f"mutant {name}: anchor not found in {path}"
        out[name] = {**files, path: files[path].replace(old, new, count)}

    edit("pool gate dropped", pm, "  count = var.enable_cognito ? 1 : 0\n  name  =", "  name  =")
    edit("gate inverted", pm, "count        = var.enable_cognito ? 1 : 0\n  name         = \"PortalClient\"",
         "count        = var.enable_cognito ? 0 : 1\n  name         = \"PortalClient\"")
    edit("group always created", pm, "count        = var.enable_cognito ? 1 : 0\n  name         = \"platform-admin\"",
         "count        = 1\n  name         = \"platform-admin\"")
    edit("gate tied to something else", "main.tf", 'enable_cognito = local.oidc_issuer == ""', "enable_cognito = true")
    edit("moved block dropped", pm, "moved {\n  from = aws_cognito_user_pool.portal\n  to   = aws_cognito_user_pool.portal[0]\n}\n", "")
    edit("null fact", po, 'try(aws_cognito_user_pool.portal[0].id, "")', "one(aws_cognito_user_pool.portal[*].id)")
    edit("admin secret back as a fact", po, "    service_entry_api_id      =",
         '    portal_admin_secret_name = "agent-platform/portal-admin"\n    service_entry_api_id      =')
    edit("runner reads a secret", ps, '  statement {\n    sid = "VpcNetworkInterfaces"',
         '  statement {\n    actions   = ["secretsmanager:GetSecretValue"]\n    resources = ["*"]\n  }\n\n'
         '  statement {\n    sid = "VpcNetworkInterfaces"')
    out["ungated pool elsewhere"] = {**files, "extra.tf": 'resource "aws_cognito_user_pool" "other" {\n  name = "x"\n}\n'}
    return out


def main() -> None:
    files = load()
    check(files)
    # 6
    hostile = mutants(files)
    for name, mutated in hostile.items():
        try:
            check(mutated, report=lambda _m: None)
        except AssertionError:
            continue
        raise AssertionError(f"checker accepted a hostile edit: {name}")
    ok(f"the checker rejects all {len(hostile)} hostile edits of the real files")
    print("one identity provider: all assertions passed")


if __name__ == "__main__":
    try:
        main()
    except AssertionError as e:
        print(f"FAIL {e}")
        sys.exit(1)
