#!/usr/bin/env python3
"""The application pipeline's namespaces enforce Pod Security Admission
(AWS Continuum C110).

The workloads deploy role may create Deployments in portal<suffix> and
llm-edge<suffix> (charts/platform-rbac Role). A namespace with no
pod-security.kubernetes.io/enforce label admits any pod spec, and the
staging namespaces share the production cluster's single node group: a
privileged or hostPath pod from the staging pipeline is root on a node that
also runs production's backend and kube-system, and reads their projected
IRSA tokens. Static (no cluster, no helm binary: CI's qa job has neither):

  1. charts/platform-rbac renders exactly one Namespace, named by the release
     namespace, carrying enforce/audit/warn and their -version labels, and the
     keep resource policy (uninstalling RBAC must not delete the workloads);
  2. values.yaml enforces baseline or restricted (never privileged, never
     empty), audits and warns at restricted, pins a vX.Y version that equals
     the cluster's minor version default (eks_kubernetes_version);
  3. every helm_release of platform-rbac renders the namespace itself
     (create_namespace = false) and adopts the existing one
     (take_ownership = true), and no values override in that release
     touches podSecurity;
  4. nothing else creates the two pipeline namespaces: no other helm_release
     into portal/llm-edge has create_namespace = true and no
     kubernetes_namespace resource exists;
  5. negative control: the same checks fail on the chart and modules as they
     were before this change (no Namespace template, create_namespace = true).

Run: python3 terraform/tests/test_namespace_pod_security_adversarial.py
"""

import os
import re
import sys

import yaml

TF = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHART = os.path.join(TF, "charts", "platform-rbac")
MODULES = os.path.join(TF, "modules")
RBAC_MODULES = ("portal/workloads.tf", "llm_edge/main.tf")
PIPELINE_NS = {"portal": "portal/workloads.tf", "llm_edge": "llm_edge/main.tf"}
PSA = "pod-security.kubernetes.io/"


def ok(msg: str) -> None:
    print(f"  ok   {msg}")


def read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def blocks(text: str, header_re: str) -> list:
    """bodies of the HCL blocks whose header matches header_re (brace-balanced)."""
    out = []
    for m in re.finditer(header_re, text, re.M):
        depth, i = 0, text.index("{", m.start())
        start = i
        while True:
            depth += text[i] == "{"
            depth -= text[i] == "}"
            i += 1
            if depth == 0:
                out.append(text[start:i])
                break
    return out


def attr(body: str, name: str):
    m = re.search(rf"^\s*{name}\s*=\s*([^\s#]+)", body, re.M)
    return m.group(1) if m else None


def chart_docs(templates: dict) -> list:
    """structural parse: Go templating replaced by a placeholder, like test_workloads_role_isolation."""
    docs = []
    for text in templates.values():
        text = re.sub(r"\{\{.*?\}\}", "TEMPLATED", text)
        docs += [d for d in yaml.safe_load_all(text) if d]
    return docs


def check_chart(templates: dict) -> None:
    namespaces = [d for d in chart_docs(templates) if d.get("kind") == "Namespace"]
    assert len(namespaces) == 1, f"platform-rbac must render exactly one Namespace, found {len(namespaces)}"
    raw = next(t for t in templates.values() if re.search(r"^kind:\s*Namespace\s*$", t, re.M))
    assert re.search(r"^\s+name:\s*\{\{\s*\.Release\.Namespace\s*\}\}\s*$", raw, re.M), \
        "the Namespace must be the release namespace, or the labels land on some other namespace"
    labels = namespaces[0]["metadata"].get("labels") or {}
    for mode in ("enforce", "audit", "warn"):
        for key in (mode, f"{mode}-version"):
            assert PSA + key in labels, f"Namespace lacks {PSA}{key}"
        assert re.search(rf"{re.escape(PSA + mode)}:\s*\{{\{{\s*required\b[^}}]*\.Values\.podSecurity\.{mode}\b", raw), \
            f"{mode} level must come from a required podSecurity.{mode} (an empty value renders a label PSA ignores)"
    annotations = namespaces[0]["metadata"].get("annotations") or {}
    assert annotations.get("helm.sh/resource-policy") == "keep", \
        "the Namespace needs helm.sh/resource-policy: keep, or uninstalling platform-rbac deletes every workload in it"


def check_values(values: dict, eks_version: str) -> None:
    ps = values.get("podSecurity") or {}
    assert ps.get("enforce") in ("baseline", "restricted"), \
        f"enforce must be baseline or restricted, got {ps.get('enforce')!r}: privileged (or absent) admits hostPath and privileged pods"
    assert ps.get("audit") == "restricted" and ps.get("warn") == "restricted", \
        f"audit and warn stay at restricted so the remaining gap stays visible: {ps}"
    version = str(ps.get("version", ""))
    assert re.fullmatch(r"v\d+\.\d+", version), \
        f"pin the policy version (vX.Y), not latest: a cluster upgrade must not tighten it under running pods: {version!r}"
    assert version == f"v{eks_version}", \
        f"podSecurity.version {version} drifted from eks_kubernetes_version {eks_version}; move them together"


def check_modules(sources: dict) -> None:
    for rel in RBAC_MODULES:
        rbac = [b for b in blocks(sources[rel], r'^resource "helm_release" "rbac" \{') if "platform-rbac" in b]
        assert len(rbac) == 1, f"{rel}: expected one platform-rbac helm_release, found {len(rbac)}"
        body = rbac[0]
        assert attr(body, "create_namespace") == "false", \
            f"{rel}: create_namespace must be false; Helm then creates the namespace without the chart's labels"
        assert attr(body, "take_ownership") == "true", \
            f"{rel}: take_ownership must be true, or the release cannot adopt the namespace that create_namespace made"
        assert "podSecurity" not in body, f"{rel}: the release overrides podSecurity; the levels are the chart's"
    for name, rel in PIPELINE_NS.items():
        text = sources[rel]
        for b in blocks(text, r'^resource "helm_release" "[^"]+" \{'):
            if "platform-rbac" in b:
                continue
            assert attr(b, "create_namespace") != "true", \
                f"{rel}: another release creates the {name} namespace; it may run first and leave it unlabelled"
    for rel, text in sources.items():
        assert not re.search(r'^resource "kubernetes_namespace(_v1)?"', text, re.M), \
            f"{rel}: a kubernetes_namespace resource bypasses the chart's labels"


def load():
    templates = {f: read(os.path.join(CHART, "templates", f)) for f in sorted(os.listdir(os.path.join(CHART, "templates")))}
    values = yaml.safe_load(read(os.path.join(CHART, "values.yaml")))
    sources = {}
    for root, _, files in os.walk(MODULES):
        for f in files:
            if f.endswith(".tf"):
                p = os.path.join(root, f)
                sources[os.path.relpath(p, MODULES)] = read(p)
    var = blocks(read(os.path.join(TF, "variables.tf")), r'^variable "eks_kubernetes_version" \{')[0]
    eks_version = re.search(r'default\s*=\s*"([^"]+)"', var).group(1)
    return templates, values, sources, eks_version


def expect_failure(fn, *args, why: str) -> None:
    try:
        fn(*args)
    except AssertionError:
        return
    raise AssertionError(f"negative control passed but should fail: {why}")


def main() -> int:
    templates, values, sources, eks_version = load()

    check_chart(templates)
    ok("platform-rbac renders the release namespace with PSA enforce/audit/warn (+versions) from required values and keep")
    check_values(values, eks_version)
    ok(f"values: enforce={values['podSecurity']['enforce']}, audit/warn=restricted, version pinned to v{eks_version}")
    check_modules(sources)
    ok("both platform-rbac releases: create_namespace=false, take_ownership=true, no podSecurity override")
    ok("no other release or kubernetes_namespace creates the pipeline namespaces")

    # negative controls: the pre-C110 shape, and the weakenings a later change could make
    old_templates = {k: v for k, v in templates.items() if not re.search(r"^kind:\s*Namespace\s*$", v, re.M)}
    expect_failure(check_chart, old_templates, why="chart without a Namespace template")
    no_keep = {k: v.replace("helm.sh/resource-policy: keep", "") for k, v in templates.items()}
    expect_failure(check_chart, no_keep, why="Namespace without the keep policy")
    ok("negative: the chart check fails without the Namespace template or without keep")

    for bad in ({"enforce": "privileged"}, {"enforce": ""}, {"version": "latest"}, {"audit": "baseline"}, {"version": "v1.29"}):
        weak = {**values, "podSecurity": {**values["podSecurity"], **bad}}
        expect_failure(check_values, weak, eks_version, why=f"values {bad}")
    ok("negative: the values check fails for privileged, empty, latest, a lowered audit and a drifted version")

    old = dict(sources)
    for rel in RBAC_MODULES:
        old[rel] = re.sub(r"create_namespace\s*=\s*false[^\n]*", "create_namespace = true", old[rel])
        old[rel] = re.sub(r"\n\s*take_ownership\s*=\s*true", "", old[rel])
    expect_failure(check_modules, old, why="create_namespace = true, no take_ownership (the pre-C110 releases)")
    extra = dict(sources)
    extra["portal/workloads.tf"] += '\nresource "kubernetes_namespace" "x" {\n  metadata {\n    name = "portal"\n  }\n}\n'
    expect_failure(check_modules, extra, why="a kubernetes_namespace resource")
    ok("negative: the module check fails on the pre-C110 releases and on a kubernetes_namespace")

    print("namespace pod security: all assertions passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
