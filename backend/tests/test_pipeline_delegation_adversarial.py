"""Tests for pipeline run attribution (``api/pipelines.start_run``,
``schedule_service.delegation_body``).

A ``pipeline:<name>`` schedule fires through the schedule-runner Lambda, which
cannot run Node and therefore starts the run through the portal API signed in
as the delegation user. Before this change the run, every ``agent()``
invocation under it, the quota it consumed and the audit line were attributed
to that delegation user, while architecture.md promises attribution to the
schedule's ``created_by``. The run request now carries ``on_behalf_of``, which
only the delegation user may set.

Invariants:
    I1  without ``on_behalf_of`` the run belongs to the caller
    I2  the delegation user (settings.pipeline_delegation_users) may name
        another user; the run and the audit line carry that user, and the
        audit detail records who delegated
    I3  any other caller naming ``on_behalf_of`` gets 403 and no run starts;
        the comparison with the configured users is exact
    I4  the Lambda path sends the schedule's created_by, and nothing when the
        schedule has no owner
    I5  an empty on_behalf_of is rejected by the schema

The schedule-runner now reaches the backend through the private IAM service
entry, signed with its own role, instead of signing in as the portal admin:

    I6  ``/service/v1/pipelines/{name}/runs`` admits only a caller on
        settings.pipeline_service_callers (exact, assumed-role sessions
        normalized to the role), only with the gateway's entry secret; an
        empty allowlist admits nobody; poll is gated the same way
    I7  a service run belongs to the on_behalf_of user, which the service
        schema requires (a role has no user of its own); the audit line names
        the delegating role
    I8  with pipeline_service_url set the Lambda path signs every request
        with SigV4 for execute-api, sends the owner, and never reads the
        portal-admin secret or calls Cognito
    I9  without pipeline_service_url the previous portal path is unchanged
        (Cognito sign-in, owner in the body)
    I10 the service router is mounted in both the entry-only (data plane)
        and the management deployment
    I11 the workloads root hands the runner the endpoint-specific hostname
        ({api-id}-{vpce-id}.execute-api..., the private API has no public DNS
        and the endpoint's private DNS is off), always puts it in the platform
        VPC's private subnets, allowlists exactly the runner role on the
        backend, and gives it no portal sign-in (no portal URL, no admin
        secret, no switch back to one)
    I12 over HTTP, through the mounted router (not the helper): a missing or
        forged entry secret, a missing caller ARN or a role off the allowlist
        is refused before any run is created or read; only the allowlisted
        role with the right secret starts and polls a run
    I13 terraform refuses a runner without the private URL, or given it
        without subnets or a security group (the schedule_runner variable
        validation, evaluated)
    I14 the real application, imported in each deployment mode (entry-only
        and management), serves the pipeline routes and refuses a forged or
        foreign caller there; the real settings default to no service caller
        and no service URL, so an unconfigured backend admits nobody
    I15 one identity provider: the workloads give an OIDC backend no Cognito
        setting (in OIDC mode the backend otherwise also accepts the pool's
        ID tokens, so the pool's admin password would be a way in beside the
        realm) and Cognito only without OIDC; terraform refuses a backend with
        neither (evaluated); an OIDC backend without a pool refuses a token
        the realm does not verify, and the same token passes once a pool is
        configured (the chain this keeps unconfigured)
"""

from __future__ import annotations

import asyncio
import json
import os
import re

os.environ.setdefault("PLATFORM_AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app import config, dependencies
from app.api import pipelines as route
from app.services import schedule_service as sched


class Recorder:
    def __init__(self):
        self.runs: list[dict] = []
        self.audit: list[tuple] = []

    def start_run(self, *, name, user, args=None):
        self.runs.append({"name": name, "user": str(user), "args": args})
        return {"id": "run1", "pipeline": name, "status": "running", "started_by": str(user)}

    def record(self, user, action, resource="", detail=""):
        self.audit.append((str(user), action, resource, detail))


@pytest.fixture
def rec(monkeypatch):
    r = Recorder()
    monkeypatch.setattr(route, "pipeline_service", r)
    monkeypatch.setattr(route, "audit_service", r)
    monkeypatch.setattr(config.settings, "pipeline_delegation_users", "admin")
    return r


def principal(name: str, admin: bool = True):
    return dependencies._principal(name, {}, admin=admin)


def call(name, body, user):
    return asyncio.run(route.start_run(name, route.PipelineRunRequest(**body), user))


def test_i1_plain_run_belongs_to_the_caller(rec):
    run = call("digest", {}, principal("alice"))
    assert run["started_by"] == "alice" and rec.runs[0]["user"] == "alice"
    assert rec.audit[0][0] == "alice" and "delegated_by" not in rec.audit[0][3]


def test_i2_delegation_user_attributes_the_run_to_the_schedule_owner(rec):
    run = call("digest", {"on_behalf_of": "alice"}, principal("admin"))
    assert run["started_by"] == "alice" and rec.runs[0]["user"] == "alice"
    user, action, resource, detail = rec.audit[0]
    assert user == "alice" and action == "pipeline.run.start" and resource == "pipeline:digest"
    assert "delegated_by:admin" in detail


def test_i3_other_callers_cannot_run_as_another_user(rec, monkeypatch):
    for caller in (principal("bob"), principal("alice"), principal("Admin")):
        with pytest.raises(HTTPException) as exc:
            call("digest", {"on_behalf_of": "alice"}, caller)
        assert exc.value.status_code == 403
    assert rec.runs == [] and rec.audit == []
    monkeypatch.setattr(config.settings, "pipeline_delegation_users", "admin, scheduler-bot")
    call("digest", {"on_behalf_of": "alice"}, principal("scheduler-bot"))
    assert rec.runs[-1]["user"] == "alice"
    with pytest.raises(HTTPException):
        call("digest", {"on_behalf_of": "alice"}, principal("scheduler-bot2"))


def test_i4_lambda_sends_the_owner():
    assert sched.delegation_body("alice") == {"on_behalf_of": "alice"}
    assert sched.delegation_body("") == {}


def test_i5_empty_on_behalf_of_is_refused_by_the_schema():
    with pytest.raises(ValidationError):
        route.PipelineRunRequest(on_behalf_of="")
    assert route.PipelineRunRequest().on_behalf_of is None


# ------------------------------------------------ IAM service entry

RUNNER = "arn:aws:iam::00000000000:role/agent-platform-schedule-runner"


@pytest.fixture
def entry(monkeypatch):
    monkeypatch.setattr("app.api.service_entry._entry_secret", lambda: "s3cret")
    monkeypatch.setattr(config.settings, "pipeline_service_callers", RUNNER)


class Store(Recorder):
    def get_run_full(self, run_id):
        return {"id": run_id, "status": "completed"} if run_id == "run1" else None


def test_i6_service_route_admits_only_allowlisted_roles_with_the_entry_secret(entry, monkeypatch):
    session = "arn:aws:sts::00000000000:assumed-role/agent-platform-schedule-runner/fn-123"
    assert route._service_caller("s3cret", session) == RUNNER
    for secret, caller, status in (
        ("s3cret", "arn:aws:iam::00000000000:role/agent-platform-schedule-runner-staging", 403),
        ("s3cret", "arn:aws:iam::00000000000:role/agent-platform-schedule-runne", 403),
        ("s3cret", "arn:aws:iam::00000000001:role/agent-platform-schedule-runner", 403),
        ("s3cret", "admin", 401),
        ("wrong", RUNNER, 401),
        ("", RUNNER, 401),
    ):
        with pytest.raises(HTTPException) as exc:
            route._service_caller(secret, caller)
        assert exc.value.status_code == status, (secret, caller)
    monkeypatch.setattr(config.settings, "pipeline_service_callers", "")
    with pytest.raises(HTTPException) as exc:
        route._service_caller("s3cret", RUNNER)
    assert exc.value.status_code == 403


def test_i7_service_run_belongs_to_the_named_owner(entry, monkeypatch):
    s = Store()
    monkeypatch.setattr(route, "pipeline_service", s)
    monkeypatch.setattr(route, "audit_service", s)
    req = route.ServicePipelineRunRequest(on_behalf_of="alice")
    run = asyncio.run(route.service_start_run("digest", req, RUNNER))
    assert run["started_by"] == "alice" and s.runs == [{"name": "digest", "user": "alice", "args": None}]
    user, action, resource, detail = s.audit[0]
    assert (user, action, resource) == ("alice", "pipeline.run.start", "pipeline:digest")
    assert f"delegated_by:{RUNNER}" in detail
    assert route.service_get_run("run1", RUNNER)["status"] == "completed"
    with pytest.raises(HTTPException) as exc:
        route.service_get_run("nope", RUNNER)
    assert exc.value.status_code == 404
    for bad in ({}, {"on_behalf_of": ""}):
        with pytest.raises(ValidationError):
            route.ServicePipelineRunRequest(**bad)


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


def _no_aws_client(name, **kw):
    raise AssertionError(f"the service-entry path must not create a {name} client")


def test_i8_lambda_signs_with_its_role_and_never_touches_cognito(monkeypatch):
    monkeypatch.setattr(config.settings, "pipeline_service_url", "https://abc-vpce-1.execute-api.us-east-1.amazonaws.com/svc/")
    monkeypatch.setattr(config.settings, "portal_api_url", "https://portal.example")
    monkeypatch.setattr(sched.boto3, "client", _no_aws_client)
    monkeypatch.setattr(sched.time, "sleep", lambda s: None)
    sent = []

    def urlopen(req, timeout=None):
        sent.append(req)
        return FakeResponse({"id": "run1", "status": "running" if req.get_method() == "POST" else "completed"})

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    out = sched.ScheduleService._delegate_pipeline("digest", "alice")
    assert out["ok"] and out["status"] == "completed"
    post, poll = sent[0], sent[1]
    assert post.full_url == "https://abc-vpce-1.execute-api.us-east-1.amazonaws.com/svc/service/v1/pipelines/digest/runs"
    assert poll.full_url.endswith("/svc/service/v1/pipeline-runs/run1")
    assert json.loads(post.data) == {"on_behalf_of": "alice"}
    for req in sent:
        auth = req.get_header("Authorization") or ""
        assert auth.startswith("AWS4-HMAC-SHA256 ") and "/us-east-1/execute-api/aws4_request" in auth, auth
        assert req.get_header("X-amz-date")


def test_i9_without_the_service_url_the_portal_path_is_unchanged(monkeypatch):
    monkeypatch.setattr(config.settings, "pipeline_service_url", "")
    monkeypatch.setattr(config.settings, "portal_api_url", "https://portal.example")
    monkeypatch.setattr(sched.time, "sleep", lambda s: None)
    calls = []

    class Client:
        def get_secret_value(self, SecretId):
            calls.append(("secret", SecretId))
            return {"SecretString": json.dumps({"username": "admin", "password": "pw"})}

        def initiate_auth(self, **kw):
            calls.append(("cognito", kw["ClientId"], kw["AuthFlow"]))
            return {"AuthenticationResult": {"IdToken": "tok"}}

    monkeypatch.setattr(sched.boto3, "client", lambda name, **kw: Client())
    sent = []

    def urlopen(req, timeout=None):
        sent.append(req)
        if req.full_url.endswith("/api/v1/config"):
            return FakeResponse({"cognito_client_id": "client1"})
        return FakeResponse({"id": "run1", "status": "completed"})

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    sched.ScheduleService._delegate_pipeline("digest", "alice")
    assert ("cognito", "client1", "USER_PASSWORD_AUTH") in calls
    post = next(r for r in sent if r.get_method() == "POST")
    assert post.full_url == "https://portal.example/api/v1/pipelines/digest/runs"
    assert json.loads(post.data) == {"on_behalf_of": "alice"} and post.get_header("Authorization") == "Bearer tok"


def test_i10_service_router_is_mounted_in_both_deployments():
    src = open(os.path.join(os.path.dirname(route.__file__), "..", "main.py"), encoding="utf-8").read()
    data_plane, _, management = src.partition("\nelse:\n")
    assert "app.include_router(pipelines.service)" in data_plane.split("if settings.entry_only:")[-1]
    assert "app.include_router(pipelines.service)" in management


WORKLOADS = os.path.join(os.path.dirname(__file__), "..", "..", "terraform", "workloads")


def _tf(*parts):
    text = open(os.path.join(WORKLOADS, *parts), encoding="utf-8").read()
    return "\n".join(line.split("#", 1)[0] for line in text.splitlines())


def test_i11_workloads_wire_the_runner_to_the_private_entry():
    root = _tf("main.tf")
    wiring = root[root.index("schedule_runner = {"):]
    wiring = wiring[: wiring.index("\n  }")]
    assert re.search(
        r'service_url\s*=\s*replace\(local\.facts\.portal\.service_entry_api_url,\s*"\.execute-api\.",\s*'
        r'"-\$\{local\.facts\.portal\.service_entry_vpce_id\}\.execute-api\."\)', wiring
    ), "the runner must call the endpoint-specific hostname, not the private API's bare (unresolvable) one"
    assert "local.facts.network.private_subnet_ids" in wiring and "local.facts.network.runtime_sg_id" in wiring
    assert "schedule_runner_via_service_entry" not in root + _tf("variables.tf"), \
        "no switch back to the portal sign-in"

    portal = _tf("modules", "portal", "main.tf")
    runner = portal[portal.index('resource "aws_lambda_function" "schedule_runner"'):]
    runner = runner[: re.search(r"\n}(\n|$)", runner).end()]
    assert "dynamic \"vpc_config\"" not in runner, "the runner is always in the VPC"
    vpc = runner[runner.index("vpc_config {"):]
    vpc = vpc[: vpc.index("}") + 1]
    assert "var.schedule_runner.subnet_ids" in vpc and "[var.schedule_runner.security_group_id]" in vpc
    assert re.search(r"PLATFORM_PIPELINE_SERVICE_URL\s*=\s*var\.schedule_runner\.service_url", runner)
    for gone in ("PLATFORM_PORTAL_API_URL", "PLATFORM_PORTAL_ADMIN_SECRET", "portal_admin_secret", "COGNITO"):
        assert gone not in runner, f"the runner carries no portal sign-in: {gone}"
    callers = re.search(r"PLATFORM_PIPELINE_SERVICE_CALLERS\s*=\s*(.+)", portal).group(1).strip()
    assert callers == "var.portal.schedule_runner_role_arn", callers

    variables = _tf("modules", "portal", "variables.tf")
    sr = variables[variables.index('variable "schedule_runner"'):]
    sr = sr[: sr.index("\n}\n")]
    assert "default" not in sr, "the module has no runner without the service entry"
    assert re.search(r'condition\s*=\s*var\.schedule_runner\.service_url != ""\s*&&', sr), \
        "an empty service URL must fail the plan"
    assert "portal_admin_secret_name" not in variables


def test_i12_http_refuses_before_any_run_is_touched(entry, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    s = Store()
    monkeypatch.setattr(route, "pipeline_service", s)
    monkeypatch.setattr(route, "audit_service", s)
    touched = []
    real_get = s.get_run_full
    monkeypatch.setattr(s, "get_run_full", lambda run_id: touched.append(run_id) or real_get(run_id))
    app = FastAPI()
    app.include_router(route.service)
    client = TestClient(app)
    start, poll = "/service/v1/pipelines/digest/runs", "/service/v1/pipeline-runs/run1"
    body = {"on_behalf_of": "alice"}
    good = {"x-service-entry-secret": "s3cret", "x-caller-arn": RUNNER}
    refused = (
        ({}, 401),
        ({"x-caller-arn": RUNNER}, 401),
        ({"x-service-entry-secret": "wrong", "x-caller-arn": RUNNER}, 401),
        ({"x-service-entry-secret": "s3cret"}, 401),
        ({"x-service-entry-secret": "s3cret", "x-caller-arn": "arn:aws:iam::00000000000:role/someone-else"}, 403),
        ({"x-service-entry-secret": "s3cret", "x-caller-arn": RUNNER + "-staging"}, 403),
    )
    for headers, status in refused:
        assert client.post(start, json=body, headers=headers).status_code == status, headers
        assert client.get(poll, headers=headers).status_code == status, headers
    assert s.runs == [] and s.audit == [] and touched == [], "a refused request reached the run store"
    r = client.post(start, json=body, headers=good)
    assert r.status_code == 200 and s.runs == [{"name": "digest", "user": "alice", "args": None}]
    assert client.post(start, json={}, headers=good).status_code == 422, "on_behalf_of is required"
    assert client.get(poll, headers=good).json()["status"] == "completed"


def _variable_block(text: str, name: str) -> str:
    start = text.index(f'variable "{name}"')
    depth = 0
    for i in range(text.index("{", start), len(text)):
        depth += {"{": 1, "}": -1}.get(text[i], 0)
        if depth == 0:
            return text[start:i + 1]
    raise AssertionError(f"unterminated variable {name}")


def test_i13_terraform_refuses_a_private_url_without_a_network():
    import shutil
    import subprocess
    import tempfile

    tf = shutil.which("terraform")
    if not tf:
        pytest.skip("terraform not on PATH")
    text = open(os.path.join(WORKLOADS, "modules", "portal", "variables.tf"), encoding="utf-8").read()
    start = text.index('variable "schedule_runner"')
    depth, end = 0, None
    for i in range(text.index("{", start), len(text)):
        depth += {"{": 1, "}": -1}.get(text[i], 0)
        if depth == 0:
            end = i + 1
            break
    work = tempfile.mkdtemp(prefix="runner-net-")
    try:
        with open(os.path.join(work, "variables.tf"), "w", encoding="utf-8") as fh:
            fh.write(text[start:end] + "\n")
        subprocess.run([tf, "init", "-input=false", "-backend=false"], cwd=work, check=True, capture_output=True)

        def accepted(value: dict) -> bool:
            with open(os.path.join(work, "case.tfvars.json"), "w", encoding="utf-8") as fh:
                json.dump({"schedule_runner": value}, fh)
            r = subprocess.run([tf, "plan", "-input=false", "-no-color", "-var-file=case.tfvars.json"],
                               cwd=work, capture_output=True, text=True)
            if r.returncode == 0:
                return True
            assert "Invalid value for variable" in r.stderr, r.stderr[-800:]
            return False

        url = "https://api-vpce-1.execute-api.us-east-1.amazonaws.com/svc/"
        assert not accepted({"subnet_ids": [], "security_group_id": "sg-1", "service_url": url})
        assert not accepted({"subnet_ids": ["subnet-1"], "security_group_id": "", "service_url": url})
        assert not accepted({"subnet_ids": [], "security_group_id": "", "service_url": url})
        assert accepted({"subnet_ids": ["subnet-1"], "security_group_id": "sg-1", "service_url": url})
        assert not accepted({"subnet_ids": [], "security_group_id": "", "service_url": ""}), \
            "no runner without the service entry: the portal sign-in is not this deployment's path"
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _fresh_app(monkeypatch, entry_only: bool):
    import importlib
    import sys

    monkeypatch.setattr(config.settings, "entry_only", entry_only)
    sys.modules.pop("app.main", None)
    return importlib.import_module("app.main").app


def test_i14_both_real_deployments_refuse_forged_callers(monkeypatch):
    from fastapi.testclient import TestClient

    fresh = config.Settings()
    assert fresh.pipeline_service_callers == "" and fresh.pipeline_service_url == "", \
        "an unconfigured backend must allow no service caller and the runner must default to no service path"
    monkeypatch.setattr("app.api.service_entry._entry_secret", lambda: "s3cret")
    s = Store()
    monkeypatch.setattr(route, "pipeline_service", s)
    monkeypatch.setattr(route, "audit_service", s)
    try:
        for entry_only in (True, False):
            client = TestClient(_fresh_app(monkeypatch, entry_only))
            paths = set(client.app.openapi()["paths"])
            assert {"/service/v1/pipelines/{name}/runs", "/service/v1/pipeline-runs/{run_id}"} <= paths, (entry_only, sorted(p for p in paths if "pipeline" in p))
            if entry_only:
                assert "/api/v1/pipelines/{name}/runs" not in paths, "the data plane must not expose the portal route"
            body = {"on_behalf_of": "alice"}
            monkeypatch.setattr(config.settings, "pipeline_service_callers", "")
            r = client.post("/service/v1/pipelines/digest/runs", json=body,
                            headers={"x-service-entry-secret": "s3cret", "x-caller-arn": RUNNER})
            assert r.status_code == 403, (entry_only, "empty allowlist", r.status_code)
            monkeypatch.setattr(config.settings, "pipeline_service_callers", RUNNER)
            r = client.post("/service/v1/pipelines/digest/runs", json=body,
                            headers={"x-service-entry-secret": "forged", "x-caller-arn": RUNNER})
            assert r.status_code == 401, (entry_only, "forged secret", r.status_code)
            assert s.runs == [], entry_only
            r = client.post("/service/v1/pipelines/digest/runs", json=body,
                            headers={"x-service-entry-secret": "s3cret", "x-caller-arn": RUNNER})
            assert r.status_code == 200 and s.runs[-1]["user"] == "alice", entry_only
            s.runs.clear()
    finally:
        import sys

        sys.modules.pop("app.main", None)


def test_i15_one_identity_provider(monkeypatch):
    import shutil
    import subprocess
    import tempfile

    import jwt as pyjwt

    # the workloads: Cognito settings only on the branch without an issuer
    portal = _tf("modules", "portal", "main.tf")
    env = portal[portal.index("backend_env = merge("):]
    env = env[: env.index("\n  )\n")]
    oidc_on, _, cognito_side = env.partition('var.oidc.issuer != "" ? {')[2].partition("} : {")
    assert "PLATFORM_OIDC_ISSUER" in oidc_on and "COGNITO" not in oidc_on
    assert re.search(r"PLATFORM_COGNITO_POOL_ID\s*=\s*var\.portal\.cognito_pool_id", cognito_side)
    assert portal.count("PLATFORM_COGNITO_") == 2, "the Cognito settings appear once, on the branch without OIDC"

    # terraform refuses a backend with neither provider (evaluated)
    tf = shutil.which("terraform")
    if tf:
        text = open(os.path.join(WORKLOADS, "modules", "portal", "variables.tf"), encoding="utf-8").read()
        work = tempfile.mkdtemp(prefix="one-idp-")
        try:
            with open(os.path.join(work, "variables.tf"), "w", encoding="utf-8") as fh:
                fh.write(_variable_block(text, "portal") + "\n" + _variable_block(text, "oidc") + "\n")
            subprocess.run([tf, "init", "-input=false", "-backend=false"], cwd=work, check=True, capture_output=True)
            base = {k: "x" for k in (
                "namespace", "backend_role_arn", "service_security_group_id", "backend_target_group_arn",
                "entry_target_group_arn", "cloudfront_domain", "scheduler_group", "scheduler_role_arn",
                "schedule_runner_role_arn", "schedule_runner_log_group", "schedule_runner_function",
                "schedule_dlq_arn", "service_entry_secret_name", "service_entry_api_id")}

            def accepted(portal_extra: dict, issuer: str) -> bool:
                with open(os.path.join(work, "case.tfvars.json"), "w", encoding="utf-8") as fh:
                    json.dump({"portal": {**base, **portal_extra},
                               "oidc": {"issuer": issuer, "client_id": "c", "audience": "a"}}, fh)
                r = subprocess.run([tf, "plan", "-input=false", "-no-color", "-var-file=case.tfvars.json"],
                                   cwd=work, capture_output=True, text=True)
                if r.returncode == 0:
                    return True
                assert "Invalid value for variable" in r.stderr, r.stderr[-800:]
                return False

            pool = {"cognito_pool_id": "pool", "cognito_client_id": "client"}
            assert accepted({}, "https://idp.example.invalid/realms/x"), "an OIDC foundation may publish no pool"
            assert accepted(pool, ""), "Cognito-only stays a deployment mode"
            assert not accepted({}, ""), "a backend with no identity provider"
            assert not accepted({"cognito_pool_id": "pool"}, ""), "half a pool is no provider"
        finally:
            shutil.rmtree(work, ignore_errors=True)

    # the backend: an OIDC deployment without a pool admits no Cognito token
    def realm_refuses(token):
        raise pyjwt.InvalidTokenError("not the realm's")

    monkeypatch.setattr(dependencies, "verify_oidc_token", realm_refuses)
    monkeypatch.setattr(dependencies, "verify_cognito_token", lambda token: {"cognito:username": "admin", "sub": "s"})
    for k, v in {"oidc_issuer": "https://idp.example.invalid/realms/x", "oidc_audience": "a",
                 "cognito_pool_id": "", "cognito_client_id": ""}.items():
        monkeypatch.setattr(config.settings, k, v)
    with pytest.raises(HTTPException) as exc:
        dependencies.get_current_user(authorization="Bearer pool-id-token")
    assert exc.value.status_code == 401
    # control: the chain the workloads keep unconfigured does admit it
    monkeypatch.setattr(config.settings, "cognito_pool_id", "pool")
    monkeypatch.setattr(config.settings, "cognito_client_id", "client")
    assert str(dependencies.get_current_user(authorization="Bearer pool-id-token")) == "admin"
