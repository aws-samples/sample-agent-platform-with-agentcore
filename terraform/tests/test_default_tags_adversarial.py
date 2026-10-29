#!/usr/bin/env python3
"""Every foundation resource carries the account's five required tags, and a
deployment can replace them with its own scheme.

Renders the real tagging wiring offline: the root's `tags` variable block and
`common_tags` local, copied into a scratch root whose AWS provider (dummy
credentials, nothing reaches AWS) has the same default_tags, with one
aws_ssm_parameter that sets its own tag. Checks:

  - the aws provider in versions.tf sets default_tags { tags = local.common_tags },
    and it is the root's only aws provider block (modules inherit it);
  - with no input: Project = agent-platform<name_suffix>, Environment =
    var.environment, Owner, ManagedBy = terraform, Lifecycle, plus the
    resource's own tags;
  - var.tags overrides a default key and adds new ones;
  - an empty or whitespace value is rejected;
  - no resource re-states a default key with a value of its own (two sources
    for one tag drift apart).

Run: python3 terraform/tests/test_default_tags_adversarial.py
"""

import json
import os
import re
import shutil
import subprocess  # nosec B404 - fixed argv
import sys
import tempfile

TF = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KEYS = {"Project", "Environment", "Owner", "ManagedBy", "Lifecycle"}

PROVIDER = '''
terraform {
  required_providers {
    aws = { source = "hashicorp/aws", version = ">= 6.50.0, < 7.0.0" }
  }
}
variable "name_suffix" {
  type    = string
  default = ""
}
variable "environment" {
  type    = string
  default = "production"
}
provider "aws" {
  region                      = "ap-northeast-1"
  access_key                  = "x"
  secret_key                  = "x"
  skip_credentials_validation = true
  skip_requesting_account_id  = true
  skip_metadata_api_check     = true
  default_tags {
    tags = local.common_tags
  }
}
resource "aws_ssm_parameter" "p" {
  name  = "/tag-probe"
  type  = "String"
  value = "v"
  tags  = { Name = "probe" }
}
'''


def ok(msg: str) -> None:
    print(f"  ok   {msg}")


def _block(text: str, header: str) -> str:
    start = text.index(header)
    depth, i = 0, text.index("{", start)
    while True:
        depth += text[i] == "{"
        depth -= text[i] == "}"
        i += 1
        if depth == 0:
            return text[start:i]


def _common_tags(main_tf: str) -> str:
    m = re.search(r"common_tags\s*=\s*merge\(", main_tf)
    assert m, "main.tf must define local.common_tags = merge(...)"
    depth, i = 0, m.end() - 1
    while True:
        depth += main_tf[i] == "("
        depth -= main_tf[i] == ")"
        i += 1
        if depth == 0:
            return main_tf[m.start():i]


def _plan(scratch: str, *args: str):
    """tags_all of the probe, or None if a variable was rejected."""
    env = {**os.environ, "TF_IN_AUTOMATION": "1"}
    p = subprocess.run(["terraform", f"-chdir={scratch}", "plan", "-input=false", "-no-color", "-out=t.plan", *args],
                       capture_output=True, text=True, timeout=300, env=env, check=False)  # nosec B603
    if p.returncode != 0:
        if "Invalid value for input variable" in p.stderr or "Invalid value for variable" in p.stderr:
            return None
        raise RuntimeError(p.stderr[-2000:])
    show = subprocess.run(["terraform", f"-chdir={scratch}", "show", "-json", "t.plan"],
                          capture_output=True, text=True, timeout=120, env=env, check=True)  # nosec B603
    res = json.loads(show.stdout)["planned_values"]["root_module"]["resources"]
    return res[0]["values"]["tags_all"]


def tag_violations(text: str) -> list:
    """Default keys a `tags = { ... }` map in this text sets (brace-matched, comments dropped)."""
    text = "\n".join(line.split("#", 1)[0] for line in text.splitlines())
    found = []
    for m in re.finditer(r"^\s*tags\s*=\s*\{", text, re.M):
        depth, i = 0, m.end() - 1
        while True:
            depth += text[i] == "{"
            depth -= text[i] == "}"
            i += 1
            if depth == 0:
                break
        body = text[m.end():i - 1]
        found += [k for k in sorted(KEYS) if re.search(rf'^\s*"?{k}"?\s*=', body, re.M)]
    return found


def resource_tag_violations() -> list:
    found = []
    for dirpath, _, files in os.walk(TF):
        if "/.terraform" in dirpath or "/tests" in dirpath:
            continue
        for f in files:
            if f.endswith(".tf"):
                path = os.path.join(dirpath, f)
                found += [f"{os.path.relpath(path, TF)}: tags sets {k}" for k in tag_violations(open(path, encoding="utf-8").read())]
    return found


def main() -> int:
    if not shutil.which("terraform"):
        print("terraform not on PATH: cannot render the tags")
        return 2
    versions = open(os.path.join(TF, "versions.tf"), encoding="utf-8").read()
    main_tf = open(os.path.join(TF, "main.tf"), encoding="utf-8").read()
    variables = open(os.path.join(TF, "variables.tf"), encoding="utf-8").read()

    root_tf = "".join(open(os.path.join(TF, f), encoding="utf-8").read() for f in os.listdir(TF) if f.endswith(".tf"))
    assert len(re.findall(r'^provider "aws"', root_tf, re.M)) == 1, "the root must have exactly one aws provider block"
    prov = _block(versions, 'provider "aws"')
    assert re.search(r"default_tags\s*\{\s*tags\s*=\s*local\.common_tags\s*\}", prov), \
        "provider aws must set default_tags { tags = local.common_tags }"
    ok("the root's single aws provider sets default_tags from local.common_tags")

    with tempfile.TemporaryDirectory() as tmp:
        with open(os.path.join(tmp, "main.tf"), "w", encoding="utf-8") as f:
            f.write(PROVIDER)
            f.write(_block(variables, 'variable "tags"') + "\n")
            f.write("locals {\n  " + _common_tags(main_tf) + "\n}\n")
        args = ["terraform", f"-chdir={tmp}", "init", "-backend=false", "-input=false", "-no-color"]
        installed = os.path.join(TF, ".terraform", "providers")
        if os.path.isdir(os.path.join(installed, "registry.terraform.io", "hashicorp", "aws")):
            args.append(f"-plugin-dir={installed}")
        subprocess.run(args, capture_output=True, text=True, timeout=600, check=True)  # nosec B603

        t = _plan(tmp)
        assert KEYS <= set(t), f"default tags missing: {sorted(KEYS - set(t))}"
        assert t["Project"] == "agent-platform" and t["Environment"] == "production" and t["ManagedBy"] == "terraform", t
        assert t["Name"] == "probe", "a resource's own tags must survive the merge"
        t = _plan(tmp, "-var=name_suffix=-staging", "-var=environment=staging")
        assert t["Project"] == "agent-platform-staging" and t["Environment"] == "staging", t
        ok("defaults: the five required keys, Project follows name_suffix, Environment follows environment, resource tags kept")

        t = _plan(tmp, '-var=tags={Owner="ops-team",CostCenter="cc-42"}')
        assert t["Owner"] == "ops-team" and t["CostCenter"] == "cc-42" and t["ManagedBy"] == "terraform", t
        ok("var.tags overrides a default key and adds new ones")

        for bad in ('{Owner=""}', '{Owner="  "}'):
            assert _plan(tmp, f"-var=tags={bad}") is None, f"tags = {bad} must be rejected"
        ok("an empty or blank tag value is rejected")

    bad = 'resource "x" "y" {\n  tags = {\n    Name = "n"\n    extra = { a = "b" }\n    "Owner" = "me"\n  }\n}\n'
    assert tag_violations(bad) == ["Owner"], "a default key after a nested map must still be found"
    assert not tag_violations('resource "x" "y" {\n  tags = {\n    Name = "n" # Project = "x"\n  }\n}\n'), "a commented key is not a tag"
    found = resource_tag_violations()
    assert not found, "\n".join(found)
    ok("no resource re-states a default tag key in its own tags")

    print("default tags: all assertions passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
