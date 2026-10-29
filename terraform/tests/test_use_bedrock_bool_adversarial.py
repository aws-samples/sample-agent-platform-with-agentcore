#!/usr/bin/env python3
"""use_bedrock is a bool, and the tfvars already deployed keep working.

It used to be a string compared with == "1" (so "true" silently meant no
Bedrock). The production tfvars in SSM still say use_bedrock = "1". Executes
the real root variable block (copied into a provider-less scratch module and
run through `terraform plan`) and checks the wiring:

  - "1" and "0" (the deployed spelling) parse as true / false, as do true /
    false; "", "yes" and 1 (a bare number) are rejected rather than read as
    one of them;
  - the root default is true (Bedrock, as before);
  - modules/team_demo declares model_env.use_bedrock as bool and sets
    CLAUDE_CODE_USE_BEDROCK only on it, with no string comparison left.

Run: python3 terraform/tests/test_use_bedrock_bool_adversarial.py
"""

import os
import re
import shutil
import subprocess  # nosec B404 - fixed argv
import sys
import tempfile

TF = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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


def _value(scratch: str, literal: str):
    """The planned output for `use_bedrock = <literal>` in a tfvars file; None if rejected."""
    with open(os.path.join(scratch, "x.tfvars"), "w", encoding="utf-8") as f:
        f.write(f"use_bedrock = {literal}\n")
    proc = subprocess.run(["terraform", f"-chdir={scratch}", "plan", "-input=false", "-no-color", "-var-file=x.tfvars"],
                          capture_output=True, text=True, timeout=120, check=False)  # nosec B603
    if proc.returncode != 0:
        if "Invalid value for input variable" not in proc.stderr:
            raise RuntimeError(f"plan failed for another reason:\n{proc.stderr}")
        return None
    m = re.search(r"\+ v\s*=\s*(true|false)", proc.stdout)
    assert m, proc.stdout
    return m.group(1) == "true"


def main() -> int:
    if not shutil.which("terraform"):
        print("terraform not on PATH: cannot run the check")
        return 2
    root = _block(open(os.path.join(TF, "variables.tf"), encoding="utf-8").read(), 'variable "use_bedrock"')
    assert re.search(r"type\s*=\s*bool", root), "use_bedrock must be type = bool"
    assert re.search(r"default\s*=\s*true\b", root), "use_bedrock must default to true"
    ok("use_bedrock is a bool defaulting to true")

    with tempfile.TemporaryDirectory() as tmp:
        with open(os.path.join(tmp, "main.tf"), "w", encoding="utf-8") as f:
            f.write(root + '\noutput "v" { value = var.use_bedrock }\n')
        subprocess.run(["terraform", f"-chdir={tmp}", "init", "-backend=false", "-input=false", "-no-color"],
                       capture_output=True, text=True, timeout=120, check=True)  # nosec B603
        for literal, want in (('"1"', True), ('"0"', False), ("true", True), ("false", False)):
            got = _value(tmp, literal)
            assert got is want, f"use_bedrock = {literal} gave {got}, want {want}"
        ok('"1" / "0" (the deployed tfvars) and true / false parse as before')
        for literal in ('""', '"yes"', "1"):
            assert _value(tmp, literal) is None, f"use_bedrock = {literal} must be rejected"
        ok('"", "yes" and a bare 1 are rejected, not guessed')

    tv = open(os.path.join(TF, "modules", "team_demo", "variables.tf"), encoding="utf-8").read()
    tm = open(os.path.join(TF, "modules", "team_demo", "main.tf"), encoding="utf-8").read()
    assert re.search(r"use_bedrock\s*=\s*bool", _block(tv, 'variable "model_env"')), "model_env.use_bedrock must be bool"
    assert re.search(r"var\.model_env\.use_bedrock\s*\?\s*\{\s*CLAUDE_CODE_USE_BEDROCK\s*=\s*\"1\"\s*\}\s*:\s*\{\}", tm), \
        "team_demo must set CLAUDE_CODE_USE_BEDROCK from the bool"
    assert not re.search(r"use_bedrock\s*[!=]=", tm), "no string comparison on use_bedrock may remain"
    ok("team_demo takes a bool and sets CLAUDE_CODE_USE_BEDROCK only when it is true")

    print("use_bedrock bool: all assertions passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
