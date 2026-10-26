#!/usr/bin/env python3
"""The backend's EventBridge Scheduler grants (modules/portal/workloads.tf).

The backend mirrors every schedule into one schedule group, and on startup
reconciles DynamoDB with that group (backend/app/services/schedule_service.py
in the application repository). The reconcile starts with ListSchedules, an
action with no resource type: granted on the group's schedule ARN it never
matched, so it was AccessDenied in every environment (CloudTrail, 2026-10-22)
and the reconcile died in a background task without a log line. A schedule
present in DynamoDB but not in EventBridge (a copied table, a failed write)
was then never recreated and silently stopped running.

Invariants:
    I1  ListSchedules is granted on "*" in a statement of its own (nothing
        else may ride along on "*")
    I2  the schedule CRUD actions stay on this group's schedule ARN, never
        "*" or another group, and include neither ListSchedules nor any
        group-level action
    I3  PassRole is the scheduler role only, and only to scheduler.amazonaws.com
    I4  terraform validate passes (when terraform is available)

Run: python3 terraform/tests/test_backend_scheduler_policy_adversarial.py
"""

import os
import re
import shutil
import subprocess  # nosec B404 - fixed argv
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FOUNDATION = os.path.join(ROOT, "terraform")
WORKLOADS = os.path.join(FOUNDATION, "modules", "portal", "workloads.tf")
GROUP_ARN = '"arn:aws:scheduler:${local.region}:${local.account}:schedule/${aws_scheduler_schedule_group.portal.name}/*"'
SCHEDULER_VALUES = r'values\s*=\s*\[\s*"scheduler\.amazonaws\.com"\s*\]'
CRUD = {"scheduler:CreateSchedule", "scheduler:UpdateSchedule", "scheduler:DeleteSchedule", "scheduler:GetSchedule"}


def ok(msg: str) -> None:
    print(f"  ok   {msg}")


def balanced(text: str, start: int) -> str:
    depth, i = 0, text.index("{", start)
    while True:
        depth += text[i] == "{"
        depth -= text[i] == "}"
        i += 1
        if depth == 0:
            return text[start:i]


def statements(text: str) -> list:
    return [balanced(text, m.start()) for m in re.finditer(r"^\s*statement\s*\{", text, re.M)]


def strings(block: str, attr: str) -> list:
    m = re.search(rf"^\s*{attr}\s*=\s*\[(.*?)\]", block, re.M | re.S)
    assert m, f"no {attr} in statement:\n{block}"
    return re.findall(r'"[^"]*"|[\w.\[\]]+', m.group(1))


def unquote(xs: list) -> set:
    return {x.strip('"') for x in xs}


def main() -> int:
    text = "\n".join(ln for ln in open(WORKLOADS, encoding="utf-8").read().splitlines() if not ln.lstrip().startswith("#"))
    sched = [s for s in statements(text) if "scheduler:" in s]
    assert sched, "no scheduler statements found"

    # I1
    lists = [s for s in sched if "scheduler:ListSchedules" in s]
    assert len(lists) == 1, f"ListSchedules must appear in exactly one statement, found {len(lists)}"
    assert unquote(strings(lists[0], "actions")) == {"scheduler:ListSchedules"}, "ListSchedules must be alone in its statement"
    assert strings(lists[0], "resources") == ['"*"'], "ListSchedules has no resource type: it must be granted on \"*\""
    assert not re.search(r"^\s*effect\s*=\s*\"Deny\"", lists[0], re.M)
    ok("ListSchedules on \"*\", alone in its statement")

    # I2
    crud = [s for s in sched if s is not lists[0]]
    assert len(crud) == 1, f"expected one schedule CRUD statement, found {len(crud)}"
    acts = unquote(strings(crud[0], "actions"))
    assert acts == CRUD, f"CRUD statement actions must be exactly {sorted(CRUD)}, got {sorted(acts)}"
    res = strings(crud[0], "resources")
    assert res == [GROUP_ARN], f"CRUD must stay on this group's schedule ARN, got {res}"
    assert not any(a.endswith(("ScheduleGroup", "ScheduleGroups", "TagResource")) for a in acts)
    ok("schedule CRUD stays on this group's schedule ARN; no list or group-level action rides along")

    # I3
    # the service is matched as the condition's exact value list, not as a substring
    pass_role = [s for s in statements(text) if "iam:PassRole" in s and re.search(SCHEDULER_VALUES, s)]
    assert len(pass_role) == 1, "one PassRole statement for the scheduler"
    assert strings(pass_role[0], "resources") == ["aws_iam_role.scheduler.arn"], "PassRole only on the scheduler role"
    assert re.search(r'variable\s*=\s*"iam:PassedToService"', pass_role[0]) and re.search(r'test\s*=\s*"StringEquals"', pass_role[0])
    ok("PassRole: the scheduler role only, to scheduler.amazonaws.com only")

    # I4
    if shutil.which("terraform"):
        for args in (["init", "-backend=false", "-input=false", "-no-color"], ["validate", "-no-color"]):
            p = subprocess.run(["terraform", f"-chdir={FOUNDATION}", *args], capture_output=True, text=True, timeout=600, check=False)  # nosec B603
            assert p.returncode == 0, f"terraform {args[0]} failed:\n{p.stdout}\n{p.stderr}"
        ok("foundation validates")
    else:
        print("  skip terraform not on PATH")

    print("backend scheduler policy: all assertions passed")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as e:
        print(f"FAIL {e}")
        sys.exit(1)
