#!/usr/bin/env python3
"""An EKS add-on changes only when its pinned version does.

The three add-ons (vpc-cni, coredns, kube-proxy) used to take
data.aws_eks_addon_version with most_recent = true: every plan resolved "the
newest build", so an add-on was upgraded inside whatever unrelated change was
deployed next, and only in production (staging joins the production cluster
and plans none of it). Asserts, on the configuration with comments removed:

  - no aws_eks_addon_version data source anywhere in the foundation: nothing
    can resolve a version at plan time;
  - every aws_eks_addon resource sets addon_version, and sets it to
    var.addon_versions.<its own add-on> (an add-on without addon_version gets
    the EKS default build, which also moves);
  - main.tf passes var.eks_addon_versions to modules/eks unchanged;
  - the real validations (root and module, run through `terraform plan` in a
    provider-less scratch module) reject an empty value, "latest", a version
    without its build, a missing leading v, trailing text and a trailing
    newline, and accept exact builds;
  - the root default names all three add-ons and passes both validations.

Run: python3 terraform/tests/test_eks_addon_pins_adversarial.py
"""

import json
import os
import re
import shutil
import subprocess  # nosec B404 - fixed argv
import sys
import tempfile

TF = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADDONS = {"vpc_cni": "vpc-cni", "coredns": "coredns", "kube_proxy": "kube-proxy"}


def ok(msg: str) -> None:
    print(f"  ok   {msg}")


def strip_comments(text: str) -> str:
    """HCL without # // and /* */ comments; string contents are kept."""
    out, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if c == '"':  # a quoted string: copy to its closing quote
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            out.append(text[i:j + 1])
            i = j + 1
        elif c == "#" or text.startswith("//", i):
            while i < n and text[i] != "\n":
                i += 1
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def block_at(text: str, start: int) -> str:
    """The HCL block whose header starts at `start`, by brace matching."""
    depth, i = 0, text.index("{", start)
    while True:
        c = text[i]
        if c == '"':
            i += 1
            while text[i] != '"':
                i += 2 if text[i] == "\\" else 1
        depth += c == "{"
        depth -= c == "}"
        i += 1
        if depth == 0:
            return text[start:i]


def tf_files() -> dict:
    files = {}
    for base, dirs, names in os.walk(TF):
        dirs[:] = [d for d in dirs if d not in (".terraform", "tests")]
        for name in names:
            if name.endswith(".tf"):
                path = os.path.join(base, name)
                with open(path, encoding="utf-8") as f:
                    files[os.path.relpath(path, TF)] = strip_comments(f.read())
    return files


def scratch(tmp: str, name: str, block: str, var_name: str) -> str:
    d = os.path.join(tmp, name)
    os.makedirs(d)
    # the real block, renamed to `versions` so one tfvars file drives either copy
    body = block.replace(f'variable "{var_name}"', 'variable "versions"').replace(f"var.{var_name}", "var.versions")
    with open(os.path.join(d, "main.tf"), "w", encoding="utf-8") as f:
        f.write(body + '\noutput "n" { value = length(var.versions) }\n')
    proc = subprocess.run(["terraform", f"-chdir={d}", "init", "-backend=false", "-input=false", "-no-color"],
                          capture_output=True, text=True, timeout=120, check=False)  # nosec B603
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr)
    return d


def plan(d: str, versions) -> tuple:
    """(accepted, stderr) for one value of the variable; None = use the default."""
    args = ["terraform", f"-chdir={d}", "plan", "-input=false", "-no-color"]
    if versions is not None:
        with open(os.path.join(d, "v.tfvars.json"), "w", encoding="utf-8") as f:
            json.dump({"versions": versions}, f)
        args.append("-var-file=v.tfvars.json")
    proc = subprocess.run(args, capture_output=True, text=True, timeout=120, check=False)  # nosec B603
    if proc.returncode not in (0, 1):
        raise RuntimeError(proc.stderr)
    # a failed validation, or a value the variable's type refuses outright
    if proc.returncode == 1 and "Invalid value for variable" not in proc.stderr and "Invalid value for input variable" not in proc.stderr:
        raise RuntimeError(f"plan failed for another reason:\n{proc.stderr}")
    return proc.returncode == 0, proc.stderr


def main() -> int:
    if not shutil.which("terraform"):
        print("terraform not on PATH: cannot run the validation")
        return 2
    files = tf_files()

    # 1. nothing resolves an add-on version at plan time
    resolvers = [p for p, t in files.items() if re.search(r'data\s+"aws_eks_addon_version"', t)]
    assert not resolvers, f"aws_eks_addon_version data source (a version resolved at plan time) in: {resolvers}"
    ok("no aws_eks_addon_version data source in the foundation: no plan resolves an add-on version")

    # 2. every add-on resource takes its own pinned version
    seen = {}
    for path, text in files.items():
        for m in re.finditer(r'resource\s+"aws_eks_addon"\s+"([^"]+)"', text):
            body = block_at(text, m.start())
            name = re.search(r'^\s*addon_name\s*=\s*"([^"]+)"\s*$', body, re.M)
            assert name, f"{path}: aws_eks_addon.{m.group(1)} has no literal addon_name"
            versions = re.findall(r"^\s*addon_version\s*=\s*(.+?)\s*$", body, re.M)
            assert len(versions) == 1, f"{path}: aws_eks_addon.{m.group(1)} must set addon_version exactly once (unset means the EKS default build)"
            assert path == os.path.join("modules", "eks", "main.tf"), f"{path}: add-ons are modules/eks's"
            seen[name.group(1)] = versions[0]
    assert set(seen) == set(ADDONS.values()), f"add-ons are {sorted(seen)}, expected {sorted(ADDONS.values())}"
    for key, addon in ADDONS.items():
        assert seen[addon] == f"var.addon_versions.{key}", f"{addon}: addon_version is {seen[addon]!r}, must be var.addon_versions.{key}"
    ok("vpc-cni, coredns and kube-proxy each set addon_version = var.addon_versions.<itself>, and no other add-on exists")

    # 3. root -> module, unchanged
    main_tf = files["main.tf"]
    eks_call = block_at(main_tf, main_tf.index('module "eks"'))
    assert re.search(r"^\s*addon_versions\s*=\s*var\.eks_addon_versions\s*$", eks_call, re.M), \
        "module.eks must get addon_versions = var.eks_addon_versions unchanged"
    ok("main.tf passes var.eks_addon_versions to modules/eks unchanged")

    root_vars, mod_vars = files["variables.tf"], files[os.path.join("modules", "eks", "variables.tf")]
    root_block = block_at(root_vars, root_vars.index('variable "eks_addon_versions"'))
    mod_block = block_at(mod_vars, mod_vars.index('variable "addon_versions"'))
    assert not re.search(r"^\s*default\s*=", mod_block, re.M), "modules/eks must not default addon_versions: the root's pin is the only one"

    good = {"vpc_cni": "v1.23.3-eksbuild.1", "coredns": "v1.14.7-eksbuild.35", "kube_proxy": "v1.36.0-eksbuild.65"}
    bad = ["", "latest", "v1.23.3", "1.23.3-eksbuild.1", "v1.23-eksbuild.1", "v1.23.3-eksbuild.", "v1.23.3-eksbuild.1 || true",
           " v1.23.3-eksbuild.1", "v1.23.3-eksbuild.1\n", "v1.23.3-eksbuild.1\nlatest", "v1x23x3-eksbuild.1"]
    with tempfile.TemporaryDirectory() as tmp:
        dirs = {"root": scratch(tmp, "root", root_block, "eks_addon_versions"),
                "modules/eks": scratch(tmp, "mod", mod_block, "addon_versions")}
        for where, d in dirs.items():
            for key in ADDONS:
                for value in bad:
                    accepted, err = plan(d, {**good, key: value})
                    assert not accepted, f"{where} accepted {key}={value!r}"
                    assert "must be exact add-on builds" in err, f"{where}: {key}={value!r} rejected for another reason:\n{err}"
            accepted, err = plan(d, {k: v for k, v in good.items() if k != "coredns"})
            assert not accepted and 'attribute "coredns" is required' in err, f"{where} accepted a map without coredns:\n{err}"
        ok(f"both validations reject {len(bad)} inexact values per add-on (empty, latest, no build, no leading v, trailing text or newline) and a missing add-on")

        for where, d in dirs.items():
            assert plan(d, good)[0], f"{where} must accept exact builds"
        ok("exact builds (vMAJOR.MINOR.PATCH-eksbuild.N) pass both validations")

        # 4. the root default is a complete, valid pin
        default = re.search(r"^\s*default\s*=\s*\{([^}]*)\}", root_block, re.M | re.S)
        assert default, "eks_addon_versions must have a default (the pin itself)"
        pinned = dict(re.findall(r'^\s*(\w+)\s*=\s*"([^"]*)"\s*$', default.group(1), re.M))
        assert set(pinned) == set(ADDONS), f"the default pins {sorted(pinned)}, expected {sorted(ADDONS)}"
        assert plan(dirs["root"], None)[0], "the root default must pass its own validation"
        assert plan(dirs["modules/eks"], pinned)[0], "the root default must pass modules/eks's validation"
        ok(f"the root default pins all three add-ons to exact builds ({', '.join(f'{ADDONS[k]} {v}' for k, v in sorted(pinned.items()))})")

    print("eks add-on pins: all assertions passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
