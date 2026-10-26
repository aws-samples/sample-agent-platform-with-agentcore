#!/usr/bin/env python3
"""scripts/set_platform_version.py runs with the workloads CI role and
replays a live runtime's whole configuration to change one field. For a
sandbox runtime that configuration is its isolation: the execution role, the
VPC subnets and security group, the lifecycle and the environment. Adversarial
checks that the replay changes nothing but platformVersion, and that the
script fails closed:

  1. the update carries the runtime's role, network (subnets, security
     groups, mode), protocol, lifecycle, environment and image unchanged, and
     only platformVersion differs; no field is added that the runtime did not
     have (a PUBLIC network mode can never appear from a VPC runtime)
  2. requireServiceS3Endpoint is stripped (the service rejects an update that
     carries it), and nothing else is dropped from the network block
  3. a runtime already on the requested version gets no update, including a
     V1 runtime whose response omits the field (the service default)
  4. a runtime that settles in a *_FAILED status before the update is refused
     with no update sent
  5. an update that ends in UPDATE_FAILED exits non-zero (Terraform's
     local-exec then fails the apply instead of recording success)
  6. a version other than V1 / V2 is refused by the argument parser before
     any call
  7. a botocore that does not know platformVersion exits 2 before any
     update, so an old interpreter cannot silently send a V1 replay

The control client is a fake behind boto3.client; no network is used.

Run: python3 scripts/tests/test_set_platform_version_adversarial.py
"""

import contextlib
import copy
import importlib.util
import io
import os
import sys
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

spec = importlib.util.spec_from_file_location("set_platform_version", os.path.join(ROOT, "scripts", "set_platform_version.py"))
SPV = importlib.util.module_from_spec(spec)
spec.loader.exec_module(SPV)

UPDATE_MEMBERS = [
    "agentRuntimeId", "agentRuntimeArtifact", "roleArn", "networkConfiguration", "description",
    "authorizerConfiguration", "requestHeaderConfiguration", "protocolConfiguration",
    "lifecycleConfiguration", "metadataConfiguration", "environmentVariables",
    "filesystemConfigurations", "clientToken", "platformVersion",
]

SANDBOX = {
    "agentRuntimeId": "sandbox_runtime_v2_staging-AAAAAAAAAA",
    "agentRuntimeName": "sandbox_runtime_v2_staging",
    "agentRuntimeArn": "arn:aws:bedrock-agentcore:ap-northeast-1:<account>:runtime/sandbox_runtime_v2_staging-AAAAAAAAAA",
    "agentRuntimeVersion": "1",
    "status": "READY",
    "roleArn": "arn:aws:iam::<account>:role/sandbox-exec",
    "agentRuntimeArtifact": {"containerConfiguration": {"containerUri": "<account>.dkr.ecr.ap-northeast-1.amazonaws.com/sandbox-kernel:cust-v2"}},
    "networkConfiguration": {
        "networkMode": "VPC",
        "networkModeConfig": {"securityGroups": ["sg-sandbox"], "subnets": ["subnet-a", "subnet-b"], "requireServiceS3Endpoint": True},
    },
    "protocolConfiguration": {"serverProtocol": "HTTP"},
    "lifecycleConfiguration": {"idleRuntimeSessionTimeout": 300, "maxLifetime": 2400},
    "environmentVariables": {"WORKSPACE_ROOT": "/workspace"},
    "description": "One-shot code sandbox",
    "createdAt": "2026-10-24T00:00:00Z",
    "lastUpdatedAt": "2026-10-24T00:00:00Z",
    "workloadIdentityDetails": {"workloadIdentityArn": "arn:aws:bedrock-agentcore:::workload-identity/x"},
}


class Shape:
    def __init__(self, members):
        self.members = {m: None for m in members}


class OpModel:
    def __init__(self, members):
        self.input_shape = Shape(members)


class ServiceModel:
    def __init__(self, members):
        self._members = members

    def operation_model(self, _name):
        return OpModel(self._members)


class Meta:
    def __init__(self, members):
        self.service_model = ServiceModel(members)


class FakeControl:
    """GetAgentRuntime returns `states` in order (the last one repeats);
    UpdateAgentRuntime records the request and moves to `after_update`."""

    def __init__(self, states, after_update=None, members=UPDATE_MEMBERS):
        self.states = [copy.deepcopy(s) for s in states]
        self.after_update = after_update
        self.meta = Meta(members)
        self.updates: list[dict] = []
        self.gets = 0

    def get_agent_runtime(self, agentRuntimeId):
        self.gets += 1
        if self.updates and self.after_update is not None:
            return copy.deepcopy(self.after_update)
        state = self.states[0] if len(self.states) == 1 else self.states.pop(0)
        return copy.deepcopy(state)

    def update_agent_runtime(self, **req):
        self.updates.append(copy.deepcopy(req))
        return {"agentRuntimeVersion": "2", "status": "UPDATING"}


def run(fake, *argv):
    out, err = io.StringIO(), io.StringIO()
    with patch.object(SPV.boto3, "client", lambda *a, **k: fake), patch.object(SPV.time, "sleep", lambda _s: None), \
            patch.object(sys, "argv", ["set_platform_version.py", *argv]), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = SPV.main()
        except SystemExit as e:
            code = e.code
    return code, out.getvalue() + err.getvalue()


def ok(msg: str) -> None:
    print(f"  ok   {msg}")


def main() -> int:
    rid = SANDBOX["agentRuntimeId"]

    # 1 + 2
    done = dict(copy.deepcopy(SANDBOX), platformVersion="V2", agentRuntimeVersion="2")
    fake = FakeControl([SANDBOX], after_update=done)
    code, log = run(fake, rid, "V2", "--region", "ap-northeast-1")
    assert code == 0, f"V1 -> V2 must succeed: {code} {log}"
    assert len(fake.updates) == 1, f"exactly one update, got {len(fake.updates)}"
    req = fake.updates[0]
    assert req["platformVersion"] == "V2" and req["agentRuntimeId"] == rid
    for key in ("roleArn", "agentRuntimeArtifact", "protocolConfiguration", "lifecycleConfiguration", "environmentVariables", "description"):
        assert req.get(key) == SANDBOX[key], f"{key} changed or dropped in the replay: {req.get(key)!r} != {SANDBOX[key]!r}"
    net = req["networkConfiguration"]
    assert net["networkMode"] == "VPC", "network mode must stay VPC"
    assert net["networkModeConfig"] == {"securityGroups": ["sg-sandbox"], "subnets": ["subnet-a", "subnet-b"]}, \
        f"network block must keep subnets and security groups and drop only requireServiceS3Endpoint: {net}"
    assert set(req) - {"platformVersion"} <= set(SANDBOX), f"the replay added fields the runtime did not have: {set(req) - set(SANDBOX)}"
    for read_only in ("agentRuntimeName", "agentRuntimeArn", "status", "createdAt", "workloadIdentityDetails", "clientToken"):
        assert read_only not in req, f"{read_only} must not be replayed"
    assert SANDBOX["networkConfiguration"]["networkModeConfig"]["requireServiceS3Endpoint"] is True, "fixture untouched"
    ok("replay keeps role, image, VPC subnets/SG, protocol, lifecycle and environment; only platformVersion changes")
    ok("requireServiceS3Endpoint stripped, nothing else dropped from the network block")

    # 3
    on_v2 = dict(copy.deepcopy(SANDBOX), platformVersion="V2")
    fake = FakeControl([on_v2])
    code, _ = run(fake, rid, "V2")
    assert code == 0 and not fake.updates, "a runtime already on V2 must not be updated"
    fake = FakeControl([SANDBOX])  # no platformVersion in the response = V1
    code, _ = run(fake, rid, "V1")
    assert code == 0 and not fake.updates, "a runtime reporting no platformVersion is V1 and must not be updated to V1"
    ok("already on the requested version (including the implicit V1): no update")

    # 4
    failed = dict(copy.deepcopy(SANDBOX), status="UPDATE_FAILED")
    fake = FakeControl([failed])
    code, log = run(fake, rid, "V2")
    assert code != 0 and not fake.updates, f"a runtime in UPDATE_FAILED must be refused with no update: {code} {log}"
    fake = FakeControl([dict(copy.deepcopy(SANDBOX), status="CREATE_FAILED")])
    code, _ = run(fake, rid, "V2")
    assert code != 0 and not fake.updates
    ok("a runtime settled in *_FAILED is refused before any update")

    # 5
    fake = FakeControl([SANDBOX], after_update=dict(copy.deepcopy(SANDBOX), status="UPDATE_FAILED", failureReason="health check"))
    code, log = run(fake, rid, "V2")
    assert len(fake.updates) == 1 and code != 0, f"an update ending in UPDATE_FAILED must exit non-zero: {code}"
    assert "health check" in log, "the failure reason is reported"
    ok("an update ending in UPDATE_FAILED exits non-zero with the reason")

    # 6
    for bad in ("v2", "V3", "", "V2;id"):
        fake = FakeControl([SANDBOX])
        with patch.object(SPV.boto3, "client", lambda *a, **k: (_ for _ in ()).throw(AssertionError("client built for a bad version"))):
            code, _ = run(fake, rid, bad)
        assert code not in (0, None) and not fake.updates and fake.gets == 0, f"version {bad!r} must be refused before any call"
    ok("versions other than V1 / V2 refused by the parser, before any call")

    # 7
    fake = FakeControl([SANDBOX], members=[m for m in UPDATE_MEMBERS if m != "platformVersion"])
    code, log = run(fake, rid, "V2")
    assert code == 2 and not fake.updates and fake.gets == 0, f"old botocore must exit 2 before any call: {code}"
    assert "1.43.98" in log
    ok("a botocore without platformVersion exits 2 before any call")

    print("set_platform_version: all assertions passed")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as e:
        print(f"FAIL {e}")
        sys.exit(1)
