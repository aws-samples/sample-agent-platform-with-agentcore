#!/usr/bin/env python3
"""Decide whether the Helm release updates in an adoption plan are no-ops.

An imported helm_release records only what Helm knows about the release (chart
name, revision, the values Helm stored); the configuration-side attributes —
`values`, `set_sensitive`, `atomic`, `wait`, `create_namespace`, the chart
path — are not in the release, so the first plan after an import is always an
in-place update. That update is `helm upgrade` with the same chart and the
same values: nothing changes in the cluster, the release revision moves by one.

This script accepts such an update only when the values Helm holds for the
release (plan `before.metadata.values`) equal the values the configuration
renders (plan `after.values[0]` as YAML, plus every `set_sensitive` entry
applied at its dotted path) and the chart name is the one the configuration
points at. Anything else — a different image tag, replica count, env, role —
is a real change and is reported.

    python3 helm_values_equal.py <plan.json>      # exit 0: every helm update is a no-op
                                                  # exit 1: at least one differs (listed)
"""

import json
import os
import sys

import yaml


def set_path(obj, dotted, value):
    keys = dotted.split(".")
    for k in keys[:-1]:
        obj = obj.setdefault(k, {})
    obj[keys[-1]] = value


def desired_values(after):
    merged = {}
    for doc in after.get("values") or []:
        merged.update(yaml.safe_load(doc) or {})
    for s in after.get("set_sensitive") or []:
        set_path(merged, s["name"], s["value"])
    for s in after.get("set") or []:
        set_path(merged, s["name"], s["value"])
    return merged


def helm_updates(plan):
    return [rc for rc in plan.get("resource_changes", [])
            if rc["type"] == "helm_release" and rc["change"]["actions"] == ["update"]]


def classify(plan):
    """(no_ops, differing): differing items are (address, reason)."""
    no_ops, differing = [], []
    for rc in helm_updates(plan):
        before, after = rc["change"]["before"] or {}, rc["change"]["after"] or {}
        meta = (before.get("metadata") or {})
        live = json.loads(meta.get("values") or "{}") if isinstance(meta.get("values"), str) else (meta.get("values") or {})
        want = desired_values(after)
        chart_live = meta.get("chart")
        chart_want = os.path.basename((after.get("chart") or "").rstrip("/"))
        if chart_live != chart_want:
            differing.append((rc["address"], f"chart {chart_live!r} -> {chart_want!r}"))
        elif live != want:
            diff_keys = sorted(k for k in set(live) | set(want) if live.get(k) != want.get(k))
            differing.append((rc["address"], f"values differ in {diff_keys}"))
        else:
            no_ops.append(rc["address"])
    return no_ops, differing


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    plan = json.load(open(sys.argv[1], encoding="utf-8"))
    no_ops, differing = classify(plan)
    for a in no_ops:
        print(f"  helm no-op upgrade (same chart, same values): {a}")
    for a, why in differing:
        print(f"  helm REAL change: {a}: {why}")
    return 1 if differing else 0


if __name__ == "__main__":
    sys.exit(main())
