#!/usr/bin/env python3
"""Write the import blocks that adopt one environment's existing workloads into
this root's state.

    cd terraform/workloads            # root initialised for the workspace
    TF_WORKSPACE=staging python3 migrate/gen_imports.py staging
    TF_WORKSPACE=staging terraform plan -var-file=envs/staging.tfvars -out=staging.plan
    # review: N to import, 0 to add (besides the terraform_data hooks), 0 to change, 0 to destroy
    terraform apply staging.plan

What to import comes from this root's own plan without import blocks: every
resource it would create is a resource that must already exist. The import id
comes from the live account, by the fixed name the configuration gives each
resource, so the script works whether or not the foundation state still holds
the resource (the foundation forgets them with removed.tf; adopt after or
before, either order):

    aws_bedrockagentcore_agent_runtime   list-agent-runtimes, by agent_runtime_name
    aws_cloudwatch_log_delivery_source   its name
    aws_cloudwatch_log_delivery          describe-deliveries, by delivery_source_name
    helm_release                         namespace/name
    aws_lambda_function                  function_name
    random_password                      its value: from the foundation state, or from the
                                         newest earlier version of that state object in the
                                         (versioned) state bucket that still holds it

terraform_data has no import: the platform-version hooks are created afresh,
and scripts/set_platform_version.py exits without an update when the runtime
is already on the requested version.

A resource the plan would create and no live counterpart: the script fails.
Adoption is for environments that exist; a fresh environment is applied with
WORKLOADS_BOOTSTRAP=1 instead (ci/codebuild/run.sh).

The file is per environment and never committed (.gitignore); ci/tf-plan.sh
refuses to plan a non-production workspace while one is present, which is the
right behaviour for CI and why adoption is its own CodeBuild mode.
"""

import json
import os
import re
import subprocess  # nosec B404 - fixed argv
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
WORKLOADS = os.path.dirname(HERE)
FOUNDATION = os.path.dirname(WORKLOADS)
SESSION_KEY_ADDRESS = "module.portal[0].random_password.session_binding"
NOT_IMPORTABLE = {"terraform_data"}


def run(argv, cwd=None, env=None):
    return subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, check=False)  # nosec B603


def aws_json(*args):
    p = run(["aws", *args, "--output", "json"])
    if p.returncode != 0:
        raise RuntimeError(f"aws {' '.join(args)} failed: {p.stderr.strip()}")
    return json.loads(p.stdout or "{}")


# ------------------------------------------------------------- live lookups

def live_runtime_ids():
    """agent_runtime_name -> agent_runtime_id, every page."""
    out = aws_json("bedrock-agentcore-control", "list-agent-runtimes")
    return {r["agentRuntimeName"]: r["agentRuntimeId"] for r in out.get("agentRuntimes", [])}


def live_delivery_ids():
    """delivery_source_name -> delivery id."""
    out = aws_json("logs", "describe-deliveries")
    return {d["deliverySourceName"]: d["id"] for d in out.get("deliveries", [])}


def backend_config(root):
    text = open(os.path.join(root, "backend.tf"), encoding="utf-8").read()
    bucket = re.search(r'bucket\s*=\s*"([^"]+)"', text).group(1)
    key = re.search(r'\bkey\s*=\s*"([^"]+)"', text).group(1)
    return bucket, key


def state_object_key(key, workspace):
    return key if workspace == "default" else f"env:/{workspace}/{key}"


def find_in_state(state, address):
    """attributes of the single instance at `address` (module.x[0].type.name), or None."""
    m = re.match(r"^(?:(module\.[^.]+(?:\[[^\]]*\])?)\.)?([a-z0-9_]+)\.([a-z0-9_]+)$", address)
    module, rtype, name = m.group(1) or "", m.group(2), m.group(3)
    for res in state.get("resources", []):
        if res.get("mode") == "managed" and res.get("module", "") == module and res["type"] == rtype and res["name"] == name:
            inst = res.get("instances") or []
            return inst[0]["attributes"] if inst else None
    return None


def _s3_get_version(bucket, obj, version_id):
    """the object body of one version (get-object also prints metadata to stdout, so write to a file)"""
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".tfstate", delete=False) as fh:
        path = fh.name
    try:
        p = run(["aws", "s3api", "get-object", "--bucket", bucket, "--key", obj, "--version-id", version_id, path])
        if p.returncode != 0:
            raise RuntimeError(f"get-object {obj}@{version_id} failed: {p.stderr.strip()}")
        return open(path, encoding="utf-8").read()
    finally:
        os.remove(path)


def session_key_value(workspace, state_pull=None, s3_versions=None, s3_get=None):
    """The session-binding key: current foundation state first, then earlier
    versions of the same state object, newest first. Injectable for tests."""
    state_pull = state_pull or (lambda: run(["terraform", "state", "pull"], cwd=FOUNDATION).stdout)
    current = state_pull()
    if current.strip():
        attrs = find_in_state(json.loads(current), SESSION_KEY_ADDRESS)
        if attrs:
            return attrs["result"], "current foundation state"
    bucket, key = backend_config(FOUNDATION)
    obj = state_object_key(key, workspace)
    s3_versions = s3_versions or (lambda: aws_json("s3api", "list-object-versions", "--bucket", bucket, "--prefix", obj).get("Versions", []))
    s3_get = s3_get or (lambda vid: _s3_get_version(bucket, obj, vid))
    versions = [v for v in s3_versions() if v.get("Key") == obj]
    versions.sort(key=lambda v: v["LastModified"], reverse=True)
    for v in versions:
        if v.get("IsLatest"):
            continue
        attrs = find_in_state(json.loads(s3_get(v["VersionId"])), SESSION_KEY_ADDRESS)
        if attrs:
            return attrs["result"], f"foundation state version {v['VersionId']} ({v['LastModified']})"
    raise RuntimeError(f"{SESSION_KEY_ADDRESS}: not in the foundation state nor in any earlier version of s3://{bucket}/{obj}")


# --------------------------------------------------------------- the plan

def discovery_plan(env, tf_env):
    """This root's plan without import blocks: what it would create."""
    extra = ["-var-file=envs/staging.tfvars"] if env == "staging" else []
    p = run(["terraform", "plan", "-input=false", "-no-color", "-lock-timeout=120s", "-refresh=false", *extra, "-out=/tmp/adopt-discovery.plan"],
            cwd=WORKLOADS, env=tf_env)
    if p.returncode != 0:
        raise RuntimeError(f"discovery plan failed:\n{p.stderr[-2000:]}{p.stdout[-2000:]}")
    show = run(["terraform", "show", "-json", "/tmp/adopt-discovery.plan"], cwd=WORKLOADS, env=tf_env)
    return json.loads(show.stdout)


def import_blocks(plan, runtimes, deliveries, session_key):
    """(address, id) for every planned create; raises on a missing live counterpart."""
    blocks, skipped, missing = [], [], []
    for rc in plan.get("resource_changes", []):
        if rc["change"]["actions"] != ["create"]:
            continue
        addr, rtype, after = rc["address"], rc["type"], rc["change"].get("after") or {}
        if rtype in NOT_IMPORTABLE:
            skipped.append(addr)
            continue
        if rtype == "aws_bedrockagentcore_agent_runtime":
            ident = runtimes.get(after["agent_runtime_name"])
        elif rtype == "aws_cloudwatch_log_delivery_source":
            ident = after["name"]
        elif rtype == "aws_cloudwatch_log_delivery":
            ident = deliveries.get(after["delivery_source_name"])
        elif rtype == "helm_release":
            ident = f"{after['namespace']}/{after['name']}"
        elif rtype == "aws_lambda_function":
            ident = after["function_name"]
        elif rtype == "random_password":
            if addr != SESSION_KEY_ADDRESS:
                raise RuntimeError(f"no recovery rule for {addr}")
            ident = session_key
        else:
            raise RuntimeError(f"no import id rule for {rtype} ({addr})")
        if not ident:
            missing.append(addr)
            continue
        blocks.append((addr, ident))
    if missing:
        raise RuntimeError("no live counterpart for: " + ", ".join(missing) + " — this is not an existing environment; nothing written")
    return blocks, skipped


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in ("staging", "production"):
        print(__doc__)
        return 2
    env = sys.argv[1]
    want_ws = "default" if env == "production" else env
    ws = os.environ.get("TF_WORKSPACE") or run(["terraform", "workspace", "show"], cwd=WORKLOADS).stdout.strip()
    if ws != want_ws:
        print(f"refusing: workspace is '{ws}', {env} needs '{want_ws}' (set TF_WORKSPACE={want_ws})", file=sys.stderr)
        return 2
    out = os.path.join(WORKLOADS, f"imports.{env}.tf")
    if os.path.exists(out):
        os.remove(out)  # the discovery plan must not import

    try:
        plan = discovery_plan(env, {**os.environ, "TF_WORKSPACE": want_ws})
        session_key, source = session_key_value(want_ws)
        blocks, skipped = import_blocks(plan, live_runtime_ids(), live_delivery_ids(), session_key)
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    with open(out, "w", encoding="utf-8") as fh:
        fh.write(f"# Adoption of the {env} workloads. Generated by migrate/gen_imports.py;\n"
                 f"# gitignored; delete after the apply.\n\n")
        for addr, ident in blocks:
            fh.write(f"import {{\n  to = {addr}\n  id = {json.dumps(ident)}\n}}\n\n")
    print(f"wrote {out}: {len(blocks)} import blocks (session key from {source})")
    for addr, _ in blocks:
        print(f"  {addr}")
    if skipped:
        print(f"{len(skipped)} not importable (created afresh, no-op hooks):")
        for a in skipped:
            print(f"  {a}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
