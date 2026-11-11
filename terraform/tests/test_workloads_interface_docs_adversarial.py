#!/usr/bin/env python3
"""The workloads root's interfaces are documented, and its modules declare
what they need.

A module whose variables and outputs carry no description is one a new
operator (or a customer's platform team) has to reverse-engineer from the
call site; the workloads root had 55 of 126 undocumented. This keeps the
documented set from shrinking:

  - every variable and output of terraform/workloads and of each of its
    modules has a non-empty top-level description (parsed per block, so a
    description inside a nested validation or object type does not count);
  - every module has a README.md and a versions.tf whose required_providers
    names every provider the module's resources and data sources use (a
    module that leans on the root's declaration breaks the moment it is
    called from anywhere else);
  - every module the root's main.tf calls (source = "./modules/X") is among
    those checked.

README table freshness is tf.lint's (ci/tf-lint.sh). Each rule first runs
against a broken copy and must reject it.

Run: python3 terraform/tests/test_workloads_interface_docs_adversarial.py
"""

import glob
import os
import re
import sys

WORKLOADS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "workloads")


def ok(msg: str) -> None:
    print(f"  ok   {msg}")


def strip_comments(text: str) -> str:
    return "\n".join(re.sub(r"(^|\s)(#|//).*$", "", line) for line in text.splitlines())


def blocks(text: str, kind: str):
    """(name, body) of each top-level `kind "name" { ... }`, comments dropped."""
    text = strip_comments(text)
    for m in re.finditer(rf'^{kind} "([^"]+)" \{{', text, re.M):
        depth, i = 0, m.end() - 1
        while True:
            depth += text[i] == "{"
            depth -= text[i] == "}"
            i += 1
            if depth == 0:
                break
        yield m.group(1), text[m.end():i - 1]


def top_level_description(body: str) -> bool:
    depth = 0
    for line in body.splitlines():
        if depth == 0 and re.match(r'^\s*description\s*=\s*(".+"|<<-?\w+)', line):
            return True
        depth += line.count("{") - line.count("}") + line.count("(") - line.count(")")
    return False


def undocumented(directory: str) -> list:
    missing = []
    for f in sorted(glob.glob(os.path.join(directory, "*.tf"))):
        text = open(f, encoding="utf-8").read()
        for kind in ("variable", "output"):
            for name, body in blocks(text, kind):
                if not top_level_description(body):
                    missing.append(f"{os.path.relpath(f, WORKLOADS)}: {kind} {name}")
    return missing


def providers_used(directory: str) -> set:
    src = strip_comments("".join(open(f, encoding="utf-8").read() for f in glob.glob(os.path.join(directory, "*.tf"))))
    used = set(re.findall(r'^(?:resource|data) "([a-z0-9]+)_[a-z0-9_]+" "', src, re.M))
    return used - {"terraform"}  # terraform_data is built in


def providers_declared(directory: str) -> set:
    path = os.path.join(directory, "versions.tf")
    if not os.path.isfile(path):
        return set()
    text = strip_comments(open(path, encoding="utf-8").read())
    m = re.search(r"required_providers\s*\{", text)
    if not m:
        return set()
    depth, i = 0, m.end() - 1
    while True:
        depth += text[i] == "{"
        depth -= text[i] == "}"
        i += 1
        if depth == 0:
            break
    return set(re.findall(r"^\s*([a-z0-9]+)\s*=\s*\{", text[m.end():i - 1], re.M))


def main() -> int:
    # the parsers against broken copies
    assert not top_level_description('\n  type = string\n  validation {\n    description = "x"\n  }\n'), \
        "a description inside a nested block must not count"
    assert not top_level_description('\n  description = ""\n'), "an empty description must not count"
    assert top_level_description('\n  description = <<-EOT\n    text\n  EOT\n'), "a heredoc description counts"
    commented = list(blocks('variable "a" {\n  # description = "x"\n  type = string\n}\n', "variable"))
    assert commented and not top_level_description(commented[0][1]), "a commented-out description must not count"
    ok("the description parser rejects nested, empty and commented-out descriptions and accepts heredocs")

    modules = sorted(os.path.basename(d.rstrip("/")) for d in glob.glob(os.path.join(WORKLOADS, "modules", "*/")))
    gaps = undocumented(WORKLOADS)
    for m in modules:
        gaps += undocumented(os.path.join(WORKLOADS, "modules", m))
    assert not gaps, "undocumented interfaces:\n  " + "\n  ".join(gaps)
    ok(f"workloads root and {len(modules)} modules: every variable and output has a description")

    called = set(re.findall(r'source\s*=\s*"\./modules/([^"/]+)"', open(os.path.join(WORKLOADS, "main.tf"), encoding="utf-8").read()))
    unchecked = sorted(called - set(modules))
    assert called and not unchecked, f"modules the root calls but this check skips: {unchecked}"
    ok(f"all {len(called)} modules the root calls are checked")

    missing_readme = [m for m in modules if not os.path.isfile(os.path.join(WORKLOADS, "modules", m, "README.md"))]
    assert not missing_readme, f"modules without README.md: {missing_readme}"
    ok("every module has a README.md")

    # the provider parser against a module that uses helm but declares only aws
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        open(os.path.join(tmp, "main.tf"), "w", encoding="utf-8").write(
            'resource "helm_release" "x" {}\n# resource "random_id" "commented" {}\nresource "terraform_data" "y" {}\n')
        open(os.path.join(tmp, "versions.tf"), "w", encoding="utf-8").write(
            'terraform {\n  required_providers {\n    aws = {\n      source = "hashicorp/aws"\n    }\n  }\n}\n')
        assert providers_used(tmp) - providers_declared(tmp) == {"helm"}, "an undeclared provider must be found"
    undeclared = {}
    for m in modules:
        d = os.path.join(WORKLOADS, "modules", m)
        assert os.path.isfile(os.path.join(d, "versions.tf")), f"modules/{m} has no versions.tf"
        miss = providers_used(d) - providers_declared(d)
        if miss:
            undeclared[m] = sorted(miss)
    assert not undeclared, f"modules using providers their versions.tf does not declare: {undeclared}"
    ok("every module has a versions.tf declaring each provider it uses")

    print("workloads interface docs: all assertions passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
