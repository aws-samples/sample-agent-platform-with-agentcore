#!/usr/bin/env python3
"""A destroy does not take the platform's data with it unless asked to.

The control-plane table, the commit-tagged images and the access logs have no
other copy: PITR does not survive a DeleteTable, and a rollback redeploys an
image by its commit tag. CI refuses plans with deletes (destroy_guard), but an
operator's local apply has no such gate.

  1. the root's protect_stateful defaults to true and main.tf passes it to
     modules/platform unchanged
  2. in modules/platform the DynamoDB table sets
     deletion_protection_enabled = var.protect_stateful, every ECR repository
     sets force_delete = !var.protect_stateful, the access-log bucket sets
     force_destroy = !var.protect_stateful, and the workspace bucket is never
     force-destroyed
  3. no aws_ecr_repository, aws_s3_bucket or aws_dynamodb_table anywhere in the
     foundation hard-codes force_delete/force_destroy = true or
     deletion_protection_enabled = false, except the frontend bucket
     (rebuilt from source on every deploy)

Each check first runs against a broken copy and must reject it.

Run: python3 terraform/tests/test_stateful_protection_adversarial.py
"""

import os
import re
import sys

TF = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLATFORM = os.path.join(TF, "modules", "platform", "main.tf")
# rebuilt from the repo on every deploy: emptying it on destroy loses nothing
REBUILDABLE = {("portal", "aws_s3_bucket", "frontend")}


def ok(msg: str) -> None:
    print(f"  ok   {msg}")


def _strip_comments(text: str) -> str:
    out = []
    for line in text.splitlines():
        in_str = False
        for i, c in enumerate(line):
            if c == '"' and (i == 0 or line[i - 1] != "\\"):
                in_str = not in_str
            if not in_str and (c == "#" or line[i:i + 2] == "//"):
                line = line[:i]
                break
        out.append(line)
    return "\n".join(out)


def resources(text: str) -> dict:
    """(type, name) -> body, by brace matching, comments removed."""
    text = _strip_comments(text)
    out = {}
    for m in re.finditer(r'^resource "([^"]+)" "([^"]+)" \{', text, re.M):
        depth, i = 0, m.end() - 1
        while True:
            depth += text[i] == "{"
            depth -= text[i] == "}"
            i += 1
            if depth == 0:
                break
        out[(m.group(1), m.group(2))] = text[m.end():i - 1]
    return out


def attr(body: str, name: str):
    """Top-level attribute expression (depth 0 only), or None."""
    depth = 0
    for line in body.splitlines():
        if depth == 0:
            m = re.match(rf"^\s*{name}\s*=\s*(.+?)\s*$", line)
            if m:
                return m.group(1)
        depth += line.count("{") - line.count("}")
    return None


def platform_violations(text: str) -> list:
    res = resources(text)
    problems = []
    tables = [k for k in res if k[0] == "aws_dynamodb_table"]
    repos = [k for k in res if k[0] == "aws_ecr_repository"]
    if not tables or not repos or ("aws_s3_bucket", "logs") not in res or ("aws_s3_bucket", "workspace") not in res:
        return ["modules/platform no longer declares the table, the repos, the logs bucket and the workspace bucket"]
    for k in tables:
        if attr(res[k], "deletion_protection_enabled") != "var.protect_stateful":
            problems.append(f"{k}: deletion_protection_enabled must be var.protect_stateful")
    for k in repos:
        if attr(res[k], "force_delete") != "!var.protect_stateful":
            problems.append(f"{k}: force_delete must be !var.protect_stateful")
    if attr(res[("aws_s3_bucket", "logs")], "force_destroy") != "!var.protect_stateful":
        problems.append("aws_s3_bucket.logs: force_destroy must be !var.protect_stateful")
    if attr(res[("aws_s3_bucket", "workspace")], "force_destroy") not in (None, "false"):
        problems.append("aws_s3_bucket.workspace: must never be force-destroyed")
    return problems


def hardcoded_violations(module: str, text: str) -> list:
    problems = []
    for (rtype, name), body in resources(text).items():
        if (module, rtype, name) in REBUILDABLE:
            continue
        if rtype == "aws_ecr_repository" and attr(body, "force_delete") == "true":
            problems.append(f"{module}: {rtype}.{name} force_delete = true")
        if rtype == "aws_s3_bucket" and attr(body, "force_destroy") == "true":
            problems.append(f"{module}: {rtype}.{name} force_destroy = true")
        if rtype == "aws_dynamodb_table" and attr(body, "deletion_protection_enabled") == "false":
            problems.append(f"{module}: {rtype}.{name} deletion_protection_enabled = false")
    return problems


def all_files():
    for dirpath, _, files in os.walk(TF):
        if "/.terraform" in dirpath or "/tests" in dirpath:
            continue
        rel = os.path.relpath(dirpath, TF)
        parts = rel.split(os.sep)
        module = parts[1] if parts[0] == "modules" and len(parts) > 1 else "root"
        for f in files:
            if f.endswith(".tf"):
                yield module, open(os.path.join(dirpath, f), encoding="utf-8").read()


def main() -> int:
    variables = open(os.path.join(TF, "variables.tf"), encoding="utf-8").read()
    main_tf = open(os.path.join(TF, "main.tf"), encoding="utf-8").read()
    platform = open(PLATFORM, encoding="utf-8").read()

    # 1. root default and wiring
    m = re.search(r'variable "protect_stateful" \{(.*?)\n\}', variables, re.S)
    assert m, "root variable protect_stateful is missing"
    assert re.search(r"type\s*=\s*bool", m.group(1)) and re.search(r"default\s*=\s*true", m.group(1)), \
        "protect_stateful must be a bool defaulting to true"
    call = re.search(r'module "platform" \{(.*?)\n\}', main_tf, re.S)
    assert call and re.search(r"^\s*protect_stateful\s*=\s*var\.protect_stateful\s*$", call.group(1), re.M), \
        "module.platform must get protect_stateful = var.protect_stateful"
    ok("protect_stateful is a bool defaulting to true and reaches modules/platform unchanged")

    # 2. against broken copies first (built from the real file)
    broken = {
        "table unprotected": platform.replace("deletion_protection_enabled = var.protect_stateful", ""),
        "one repo force-deleted": platform.replace("force_delete = !var.protect_stateful", "force_delete = true", 1),
        "logs force-destroyed": re.sub(r"force_destroy\s*=\s*!var\.protect_stateful", "force_destroy = true", platform, count=1),
        "workspace force-destroyed": platform.replace(
            'resource "aws_s3_bucket" "workspace" {', 'resource "aws_s3_bucket" "workspace" {\n  force_destroy = true', 1),
        "protection only in a comment": platform.replace(
            "deletion_protection_enabled = var.protect_stateful", "# deletion_protection_enabled = var.protect_stateful"),
    }
    for what, text in broken.items():
        assert text != platform, f"could not build the broken copy: {what}"
        assert platform_violations(text), f"platform check accepted a copy with {what}"
    problems = platform_violations(platform)
    assert not problems, "\n".join(problems)
    ok("modules/platform: table protected, every ECR repo and the logs bucket follow protect_stateful, workspace never force-destroyed")

    # 3. no hard-coded escape anywhere
    probe = 'resource "aws_ecr_repository" "x" {\n  name = "x"\n  force_delete = true\n}\n'
    assert hardcoded_violations("demo", probe), "a hard-coded force_delete = true must be flagged"
    assert not hardcoded_violations("portal", 'resource "aws_s3_bucket" "frontend" {\n  force_destroy = true\n}\n'), \
        "the frontend bucket is the one allowed exception"
    found = [p for mod, text in all_files() for p in hardcoded_violations(mod, text)]
    assert not found, "\n".join(found)
    ok("no ECR repo, S3 bucket or DynamoDB table in the foundation hard-codes its way around the protection (frontend bucket excepted)")

    print("stateful protection: all assertions passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
