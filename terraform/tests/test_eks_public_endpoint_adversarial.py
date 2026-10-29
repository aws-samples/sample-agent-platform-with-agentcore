#!/usr/bin/env python3
"""The EKS public API endpoint is never open to the internet.

EKS treats an empty public_access_cidrs as 0.0.0.0/0, and the variable used to
default to ["0.0.0.0/0"]. Executes the real validations (the root's
eks_public_access_cidrs block and modules/eks's public_access_cidrs block,
copied into a provider-less scratch module and run through `terraform plan`):

  - an entry wider than /16 (IPv4) or /32 (IPv6), a /0 included, is rejected
    at the root, whoever owns the cluster: complementary wide ranges
    (0.0.0.0/1 + 128.0.0.0/1) cover the internet without a /0;
  - a malformed entry is rejected;
  - an empty list is rejected by modules/eks, i.e. whenever this state creates
    the cluster; the root alone accepts it (staging joins a cluster and passes
    no list);
  - a /32 list passes both;
  - the root's default is [] (never an open list), main.tf passes the root
    variable to modules/eks unchanged, and the endpoint's public_access_cidrs
    is that module variable.

Run: python3 terraform/tests/test_eks_public_endpoint_adversarial.py
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
    """The HCL block starting at `header`, by brace matching."""
    start = text.index(header)
    depth, i = 0, text.index("{", start)
    while True:
        c = text[i]
        depth += c == "{"
        depth -= c == "}"
        i += 1
        if depth == 0:
            return text[start:i]


def _plan(scratch: str, value: str) -> tuple:
    """(accepted, stderr) for one value of the variable."""
    args = ["terraform", f"-chdir={scratch}", "plan", "-input=false", "-no-color", f"-var=cidrs={value}"]
    proc = subprocess.run(args, capture_output=True, text=True, timeout=120, check=False)  # nosec B603
    if proc.returncode not in (0, 1):
        raise RuntimeError(proc.stderr)
    if proc.returncode == 1 and "Invalid value for variable" not in proc.stderr:
        raise RuntimeError(f"plan failed for another reason:\n{proc.stderr}")
    return proc.returncode == 0, proc.stderr


def _scratch(tmp: str, name: str, block: str, var_name: str) -> str:
    d = os.path.join(tmp, name)
    os.makedirs(d)
    # the real block, renamed to `cidrs` so one -var drives either copy
    body = block.replace(f'variable "{var_name}"', 'variable "cidrs"').replace(f"var.{var_name}", "var.cidrs")
    with open(os.path.join(d, "main.tf"), "w", encoding="utf-8") as f:
        f.write(body + '\noutput "n" { value = length(var.cidrs) }\n')
    proc = subprocess.run(["terraform", f"-chdir={d}", "init", "-backend=false", "-input=false", "-no-color"],
                          capture_output=True, text=True, timeout=120, check=False)  # nosec B603
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr)
    return d


def main() -> int:
    if not shutil.which("terraform"):
        print("terraform not on PATH: cannot run the validation")
        return 2
    root_vars = open(os.path.join(TF, "variables.tf"), encoding="utf-8").read()
    mod_vars = open(os.path.join(TF, "modules", "eks", "variables.tf"), encoding="utf-8").read()
    main_tf = open(os.path.join(TF, "main.tf"), encoding="utf-8").read()
    eks_main = open(os.path.join(TF, "modules", "eks", "main.tf"), encoding="utf-8").read()

    root_block = _block(root_vars, 'variable "eks_public_access_cidrs"')
    mod_block = _block(mod_vars, 'variable "public_access_cidrs"')

    # wiring: default, root -> module, module -> endpoint
    default = re.search(r"default\s*=\s*(\[[^\]]*\])", root_block)
    assert default and default.group(1).replace(" ", "") == "[]", f"root default must be [], got {default and default.group(1)}"
    eks_call = _block(main_tf, 'module "eks"')
    assert re.search(r"^\s*public_access_cidrs\s*=\s*var\.eks_public_access_cidrs\s*$", eks_call, re.M), \
        "module.eks must get public_access_cidrs = var.eks_public_access_cidrs unchanged"
    vpc = _block(_block(eks_main, 'resource "aws_eks_cluster" "this"'), "vpc_config")
    assert re.search(r"^\s*public_access_cidrs\s*=\s*var\.public_access_cidrs\s*$", vpc, re.M), \
        "aws_eks_cluster.vpc_config.public_access_cidrs must be var.public_access_cidrs"
    assert re.search(r"endpoint_public_access\s*=\s*true", vpc), "endpoint is public: the CIDR list is what limits it"
    ok("root default is [], main.tf passes it to modules/eks unchanged, and it is the endpoint's CIDR list")

    with tempfile.TemporaryDirectory() as tmp:
        root = _scratch(tmp, "root", root_block, "eks_public_access_cidrs")
        mod = _scratch(tmp, "mod", mod_block, "public_access_cidrs")

        good = '["203.0.113.10/32","198.51.100.0/24"]'
        for value in ('["0.0.0.0/0"]', '["203.0.113.10/32","0.0.0.0/0"]', '["::/0"]', '["10.0.0.0/16","::/0"]',
                      # the internet without a /0 (Security Agent on !51)
                      '["0.0.0.0/1","128.0.0.0/1"]', '["10.0.0.0/8"]', '["198.50.0.0/15"]', '["2001:db8::/31"]'):
            accepted, err = _plan(root, value)
            assert not accepted, f"root accepted {value}"
            assert "must be at least /16" in err, f"{value} rejected for another reason:\n{err}"
        ok("a /0, and any entry wider than /16 (IPv4) or /32 (IPv6), e.g. 0.0.0.0/1 + 128.0.0.0/1, is rejected at the root")

        for value in ('["198.51.0.0/16"]', '["2001:db8::/32"]', '["2001:db8::1/128"]'):
            assert _plan(root, value)[0], f"root must accept {value} (at the floor or narrower)"
        ok("/16 and narrower (IPv4), /32 and narrower (IPv6) pass")

        for value in ('["203.0.113.10"]', '["not-a-cidr"]'):
            accepted, err = _plan(root, value)
            assert not accepted and "must be CIDR blocks" in err, f"root accepted malformed {value}"
        ok("malformed entries are rejected at the root")

        accepted, _ = _plan(root, "[]")
        assert accepted, "the root must accept [] (a state that joins a cluster passes no list)"
        accepted, err = _plan(mod, "[]")
        assert not accepted and "leaves its public API endpoint open" in err, "modules/eks must reject [] (EKS would open the endpoint)"
        ok("[] passes the root but is rejected by modules/eks, i.e. whenever this state creates the cluster")

        assert _plan(root, good)[0] and _plan(mod, good)[0], f"{good} must pass both"
        ok("a list of operator /32s and /24s passes both")

    print("eks public endpoint: all assertions passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
