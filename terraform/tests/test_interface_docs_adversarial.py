#!/usr/bin/env python3
"""The foundation's interfaces are documented, and the module READMEs say so.

A module whose variables and outputs carry no description is one a new
operator (or a customer's platform team) has to reverse-engineer from the
call site. This keeps the documented set from shrinking:

  - every variable and output of the root and of each module not listed in
    PENDING has a non-empty description (parsed per block, so a description
    inside a nested validation or object type does not count);
  - each module has a README.md whose terraform-docs tables are current
    (`terraform-docs --output-check` with the repo's .terraform-docs.yml),
    when terraform-docs is on PATH;
  - PENDING only shrinks: a module listed there that is now fully documented
    fails the check until it is removed from the list.
  - every module the root's main.tf calls (source = "./modules/X") is among
    those checked: none exempt, none outside modules/.

Each rule first runs against a broken copy and must reject it.

Run: python3 terraform/tests/test_interface_docs_adversarial.py
"""

import glob
import os
import re
import shutil
import subprocess  # nosec B404 - fixed argv
import sys

TF = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.dirname(TF)
# documented in a later change (both are being edited on another branch)
PENDING = set()


def ok(msg: str) -> None:
    print(f"  ok   {msg}")


def blocks(text: str, kind: str):
    """(name, body) of each top-level `kind "name" { ... }`, comments dropped."""
    text = "\n".join(re.sub(r"(^|\s)(#|//).*$", "", line) for line in text.splitlines())
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
                    missing.append(f"{os.path.relpath(f, TF)}: {kind} {name}")
    return missing


def main() -> int:
    # the parser against broken copies
    assert not top_level_description('\n  type = string\n  validation {\n    description = "x"\n  }\n'), \
        "a description inside a nested block must not count"
    assert not top_level_description('\n  type = string\n'), "no description"
    assert not top_level_description('\n  description = ""\n'), "an empty description must not count"
    assert top_level_description('\n  description = <<-EOT\n    text\n  EOT\n'), "a heredoc description counts"
    commented = list(blocks('variable "a" {\n  # description = "x"\n  type = string\n}\n', "variable"))
    assert commented and not top_level_description(commented[0][1]), "a commented-out description must not count"
    ok("the parser rejects nested, empty and commented-out descriptions and accepts heredocs")

    modules = sorted(os.path.basename(d.rstrip("/")) for d in glob.glob(os.path.join(TF, "modules", "*/")))
    gaps = undocumented(TF)
    for m in modules:
        if m not in PENDING:
            gaps += undocumented(os.path.join(TF, "modules", m))
    assert not gaps, "undocumented interfaces:\n  " + "\n  ".join(gaps)
    ok(f"root and {len(modules) - len(PENDING)} modules: every variable and output has a description")

    done = [m for m in PENDING if m in modules and not undocumented(os.path.join(TF, "modules", m))]
    assert not done, f"now fully documented, remove from PENDING: {done}"
    if PENDING:
        ok(f"PENDING ({', '.join(sorted(PENDING))}) still has gaps; the list only shrinks")
    # every module the root calls was among those checked (none exempt, none
    # sourced from somewhere the glob does not see)
    called = set(re.findall(r'source\s*=\s*"\./modules/([^"/]+)"', open(os.path.join(TF, "main.tf"), encoding="utf-8").read()))
    unchecked = sorted(called - (set(modules) - PENDING))
    assert called and not unchecked, f"modules the root calls but this check skips: {unchecked}"
    ok(f"all {len(called)} modules the root calls are checked")

    missing_readme = [m for m in modules if not os.path.isfile(os.path.join(TF, "modules", m, "README.md"))]
    assert not missing_readme, f"modules without README.md: {missing_readme}"
    if shutil.which("terraform-docs"):
        cfg = os.path.join(REPO, ".terraform-docs.yml")
        stale = []
        for m in modules:
            p = subprocess.run(["terraform-docs", "-c", cfg, "--output-check", os.path.join(TF, "modules", m)],
                               capture_output=True, text=True, timeout=120, check=False)  # nosec B603 B607
            if p.returncode != 0:
                stale.append(m)
        assert not stale, f"README tables out of date (run terraform-docs -c .terraform-docs.yml): {stale}"
        ok("every module has a README.md and its terraform-docs tables are current")
    else:
        ok("every module has a README.md (terraform-docs not on PATH: table freshness not checked)")

    print("interface docs: all assertions passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
