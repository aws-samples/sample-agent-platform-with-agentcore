"""AgentCore gateway credentials: revocation by non-renewal.

Every AgentCore gateway credential lives 15 minutes and a session is revoked
by refusing to renew it. These tests attack that contract: what a revoked
session, a stale or forged renewal token, a holder trying to stretch a grant,
and two invocations racing on one warm session must not be able to do. The
previous design (a per-session Deny list on the caller role) is gone, so the
backend must not write IAM at all.

DynamoDB is moto. STS is stubbed: it returns credentials expiring at
``now + DurationSeconds`` and records each call, so a test can see exactly
what lifetime was asked for.
"""

import importlib
import os
import sys
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import boto3
from fastapi import HTTPException
from moto import mock_aws

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

TABLE = "agent-platform"
ROLE_ARN = "arn:aws:iam::ACCOUNTID001:role/agent-platform-gateway-caller"
SPEC = {
    "backend": "agentcore_gateway",
    "base_url": "https://gw1.gateway.bedrock-agentcore.us-east-1.amazonaws.com",
    "model": "claude",
}
HOUR = 3600


class FakeSts:
    def __init__(self):
        self.calls = []
        self.lock = threading.Lock()

    def get_caller_identity(self):
        return {"Account": "ACCOUNTID001"}

    def assume_role(self, **kw):
        with self.lock:
            self.calls.append(kw)
        exp = int(time.time()) + int(kw["DurationSeconds"])
        return {"Credentials": {
            "AccessKeyId": f"ASIA{len(self.calls)}", "SecretAccessKey": "s", "SessionToken": "t",
            "Expiration": datetime.fromtimestamp(exp, tz=timezone.utc)}}

    assume_role_with_web_identity = assume_role


class RenewalRevocationTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {
            "AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing",
            "AWS_DEFAULT_REGION": "us-east-1", "AWS_WEB_IDENTITY_TOKEN_FILE": "",
        })
        self.env.start()
        self.mock = mock_aws()
        self.mock.start()
        boto3.resource("dynamodb", region_name="us-east-1").create_table(
            TableName=TABLE,
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"},
                       {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"},
                                  {"AttributeName": "SK", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        self.mod = importlib.import_module("app.services.llm_credentials_service")
        from app.config import settings
        self.settings = patch.multiple(
            settings, agentcore_gateway_caller_role_arn=ROLE_ARN, aws_region="us-east-1",
            dynamo_table=TABLE, llm_renew_url="https://portal.example/api/v1/llm-credentials/renew",
            session_binding_secret="test-binding-secret",
        )
        self.settings.start()
        self.sts = FakeSts()
        self.svc = self.new_service()

    def tearDown(self):
        self.settings.stop()
        self.mock.stop()
        self.env.stop()

    def new_service(self):
        svc = self.mod.LlmCredentialsService.__new__(self.mod.LlmCredentialsService)
        svc.table = boto3.resource("dynamodb", region_name="us-east-1").Table(TABLE)
        svc.sts = self.sts
        svc._account_id = ""
        return svc

    def row(self, sid):
        return self.svc.table.get_item(
            Key={"PK": "LLMTOKEN", "SK": f"RSID#{sid}"}, ConsistentRead=True).get("Item")

    def age(self, sid, **fields):
        """Move a grant's clocks, as the passage of time would."""
        names = {f"#{k}": k for k in fields}
        self.svc.table.update_item(
            Key={"PK": "LLMTOKEN", "SK": f"RSID#{sid}"},
            UpdateExpression="SET " + ", ".join(f"#{k} = :{k}" for k in fields),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues={f":{k}": v for k, v in fields.items()},
        )

    def due(self, sid):
        """Let the session's renewal interval pass, as time would."""
        self.age(sid, expires_at=int(time.time()) + 120,
                 last_renewed_at=int(time.time()) - self.mod.AGENTCORE_RENEW_MIN_INTERVAL_S - 1)

    # ------------------------------------------------------------ lifetime

    def test_every_credential_is_fifteen_minutes_whatever_the_grant(self):
        for ttl_s in (60, HOUR, 9 * HOUR, 12 * HOUR):
            g = self.svc.mint_agentcore(f"sid-life-{ttl_s}", "alice", SPEC, ttl_s=ttl_s)
            self.assertEqual(self.sts.calls[-1]["DurationSeconds"], 900, ttl_s)
            self.assertLessEqual(g["expires_at"] - time.time(), 901, ttl_s)

    def test_the_grant_ceiling_follows_the_requested_lifetime(self):
        g = self.svc.mint_agentcore("sid-ceiling", "alice", SPEC, ttl_s=9 * HOUR)
        self.assertAlmostEqual(g["renew"]["grant_expires_at"], time.time() + 9 * HOUR, delta=5)
        self.assertGreaterEqual(int(self.row("sid-ceiling")["ttl"]), g["renew"]["grant_expires_at"] + 900)

    # ------------------------------------------------------------ revocation

    def test_a_revoked_session_cannot_renew(self):
        g = self.svc.mint_agentcore("sid-rev", "alice", SPEC, ttl_s=HOUR)
        self.due("sid-rev")
        self.assertIsNotNone(self.svc.renew_agentcore("sid-rev", g["renew"]["token"]), "control: renewal works before revoke")
        self.due("sid-rev")
        self.svc.revoke("sid-rev")
        self.assertIsNone(self.row("sid-rev"))
        self.assertIsNone(self.svc.renew_agentcore("sid-rev", g["renew"]["token"]))

    def test_the_workbench_refresh_path_is_revoked_the_same_way(self):
        self.svc.mint_agentcore("sid-wb", "alice", SPEC, ttl_s=HOUR)
        self.assertIsNotNone(self.svc.rotate("sid-wb"), "control: workbench refresh works before revoke")
        self.svc.revoke("sid-wb")
        self.assertIsNone(self.svc.rotate("sid-wb"))

    def test_a_reconnect_after_revoke_does_not_revive_the_old_token(self):
        old = self.svc.mint_agentcore("sid-reconnect", "alice", SPEC, ttl_s=HOUR)
        self.svc.revoke("sid-reconnect")
        new = self.svc.mint_agentcore("sid-reconnect", "alice", SPEC, ttl_s=HOUR)
        self.due("sid-reconnect")
        self.assertNotEqual(old["renew"]["token"], new["renew"]["token"])
        self.assertIsNone(self.svc.renew_agentcore("sid-reconnect", old["renew"]["token"]))

    def test_revocation_volume_has_no_ceiling(self):
        """The Deny-list design stopped applying past ~85 live revocations.
        Revocation is now a row delete, so volume is irrelevant."""
        grants = {f"sid-bulk-{i:03d}": self.svc.mint_agentcore(f"sid-bulk-{i:03d}", "alice", SPEC, ttl_s=9 * HOUR)
                  for i in range(300)}
        for sid in grants:
            self.svc.revoke(sid)
        for sid, g in grants.items():
            self.due(sid) if self.row(sid) else None
            self.assertIsNone(self.svc.renew_agentcore(sid, g["renew"]["token"]), sid)

    def test_the_backend_writes_no_iam(self):
        self.assertFalse(hasattr(self.svc, "iam"))
        src = Path(self.mod.__file__).read_text()
        for call in ("put_role_policy", "delete_role_policy", "get_role("):
            self.assertNotIn(call, src)

    def test_a_revoke_during_a_renewal_does_not_resurrect_the_grant(self):
        """renew reads the row, calls STS, then records the new credential.
        A revoke that lands while STS is answering must win: the renewal is
        discarded and no grant row reappears (C017's upsert resurrection, in
        this path)."""
        g = self.svc.mint_agentcore("sid-race-rev", "alice", SPEC, ttl_s=HOUR)
        self.due("sid-race-rev")
        real = self.sts.assume_role
        def revoke_while_minting(**kw):
            self.svc.revoke("sid-race-rev")
            return real(**kw)
        with patch.object(self.sts, "assume_role", side_effect=revoke_while_minting):
            self.assertIsNone(self.svc.renew_agentcore("sid-race-rev", g["renew"]["token"]))
        self.assertIsNone(self.row("sid-race-rev"))

    def test_a_revoke_during_a_workbench_refresh_does_not_resurrect_the_grant(self):
        self.svc.mint_agentcore("sid-race-wb", "alice", SPEC, ttl_s=HOUR)
        real = self.sts.assume_role
        def revoke_while_minting(**kw):
            self.svc.revoke("sid-race-wb")
            return real(**kw)
        with patch.object(self.sts, "assume_role", side_effect=revoke_while_minting):
            self.assertIsNone(self.svc.rotate("sid-race-wb"))
        self.assertIsNone(self.row("sid-race-wb"))

    def test_a_renewal_raced_by_a_reconnect_is_discarded(self):
        """A revoke then reconnect between the read and the write leaves a
        row with a new nonce: the old renewal must not land on it."""
        g = self.svc.mint_agentcore("sid-race-new", "alice", SPEC, ttl_s=HOUR)
        self.due("sid-race-new")
        real = self.sts.assume_role
        def revoke_and_reconnect(**kw):
            self.sts.assume_role = real
            self.svc.revoke("sid-race-new")
            self.svc.mint_agentcore("sid-race-new", "alice", SPEC, ttl_s=HOUR)
            return real(**kw)
        self.sts.assume_role = revoke_and_reconnect
        try:
            self.assertIsNone(self.svc.renew_agentcore("sid-race-new", g["renew"]["token"]))
        finally:
            self.sts.assume_role = real

    def test_malformed_input_is_refused_before_dynamodb(self):
        """Through the real route, service and (moto) DynamoDB: no hostile body
        may turn into a 500 (judge, !109: a 10,000-character id exceeded the
        1,024-byte sort-key limit)."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        api = importlib.import_module("app.api.llm_credentials")
        app = FastAPI()
        app.include_router(api.router)
        client = TestClient(app, raise_server_exceptions=False)
        g = self.svc.mint_agentcore("sid-hostile", "alice", SPEC, ttl_s=HOUR)
        real = g["renew"]["token"]
        # moto does not enforce DynamoDB's key size limit (sort key at most
        # 1,024 bytes); the real service raises ValidationException, so make
        # the table behave like it.
        from botocore.exceptions import ClientError
        real_get = self.svc.table.get_item
        def get_item(**kw):
            if len(str(kw["Key"].get("SK", "")).encode()) > 1024:
                raise ClientError({"Error": {"Code": "ValidationException",
                                             "Message": "Aggregated size of all range keys has exceeded the size limit of 1024 bytes"}}, "GetItem")
            return real_get(**kw)
        self.svc.table = MagicMock(wraps=self.svc.table)
        self.svc.table.get_item.side_effect = get_item
        self.svc.table.meta = boto3.resource("dynamodb", region_name="us-east-1").Table(TABLE).meta
        bodies = [
            {"runtime_session_id": "x" * 10_000, "token": real},
            {"runtime_session_id": "sid-hostile", "token": "f" * 10_000},
            {"runtime_session_id": "sid-hostile\x00", "token": real},
            {"runtime_session_id": "sid hostile", "token": real},
            {"runtime_session_id": "ünïcode", "token": real},
            {"runtime_session_id": ["sid-hostile"], "token": real},
            {"runtime_session_id": {"S": "sid-hostile"}, "token": real},
            {"runtime_session_id": 12345, "token": real},
            {"runtime_session_id": "sid-hostile", "token": None},
            {"runtime_session_id": "sid-hostile", "token": real.upper()},
            {},
        ]
        with patch.object(api, "llm_credentials_service", self.svc):
            answers = [client.post("/api/v1/llm-credentials/renew", json=b) for b in bodies]
        self.assertEqual({(r.status_code, r.text) for r in answers}, {(401, '{"detail":"Renewal refused"}')},
                         [(b, r.status_code) for b, r in zip(bodies, answers) if r.status_code != 401])
        self.assertIsNotNone(self.row("sid-hostile"), "hostile bodies do not touch the grant")

    # ------------------------------------------------------------ token

    def test_a_forged_or_borrowed_token_is_refused(self):
        a = self.svc.mint_agentcore("sid-a", "alice", SPEC, ttl_s=HOUR)
        b = self.svc.mint_agentcore("sid-b", "bob", SPEC, ttl_s=HOUR)
        self.due("sid-a")
        self.due("sid-b")
        real = a["renew"]["token"]
        one_off = real[:-1] + ("1" if real[-1] == "0" else "0")  # always differs from the real token
        for token in (b["renew"]["token"], "", "0" * 64, one_off, real.upper()):
            self.assertIsNone(self.svc.renew_agentcore("sid-a", token), token)
        self.assertIsNone(self.svc.renew_agentcore("sid-b", a["renew"]["token"]))

    def test_the_token_is_not_stored_in_the_table(self):
        g = self.svc.mint_agentcore("sid-store", "alice", SPEC, ttl_s=HOUR)
        self.assertNotIn(g["renew"]["token"], repr(self.row("sid-store")))

    def test_the_token_depends_on_the_binding_secret(self):
        g = self.svc.mint_agentcore("sid-secret", "alice", SPEC, ttl_s=HOUR)
        self.due("sid-secret")
        from app.config import settings
        with patch.object(settings, "session_binding_secret", "another-secret"):
            self.assertIsNone(self.svc.renew_agentcore("sid-secret", g["renew"]["token"]))

    # ------------------------------------------------------------ stretching

    def test_renewal_is_refused_past_the_grant_ceiling(self):
        g = self.svc.mint_agentcore("sid-old", "alice", SPEC, ttl_s=HOUR)
        self.age("sid-old", grant_expires_at=int(time.time()) - 1, expires_at=int(time.time()) + 60)
        self.assertIsNone(self.svc.renew_agentcore("sid-old", g["renew"]["token"]))
        self.assertIsNone(self.svc.rotate("sid-old"))

    def test_renewing_never_moves_the_ceiling(self):
        g = self.svc.mint_agentcore("sid-stretch", "alice", SPEC, ttl_s=HOUR)
        ceiling = g["renew"]["grant_expires_at"]
        for _ in range(5):
            self.due("sid-stretch")
            r = self.svc.renew_agentcore("sid-stretch", g["renew"]["token"])
            self.assertIsNotNone(r)
            self.assertLessEqual(r["renew"]["grant_expires_at"], ceiling + 1)
            self.assertLessEqual(int(self.row("sid-stretch")["grant_expires_at"]), ceiling + 1)
            self.assertEqual(self.sts.calls[-1]["DurationSeconds"], 900)
        self.svc.rotate("sid-stretch")
        self.assertLessEqual(int(self.row("sid-stretch")["grant_expires_at"]), ceiling + 1)

    def test_a_renewal_inside_the_interval_is_throttled_not_refused(self):
        """A token holder cannot spend an STS call per request, and the answer
        is "retry", never a refusal that would end a live grant."""
        g = self.svc.mint_agentcore("sid-early", "alice", SPEC, ttl_s=HOUR)
        self.due("sid-early")
        self.assertIsNotNone(self.svc.renew_agentcore("sid-early", g["renew"]["token"]))
        before = len(self.sts.calls)
        for _ in range(5):
            with self.assertRaises(self.mod.RenewThrottled) as cm:
                self.svc.renew_agentcore("sid-early", g["renew"]["token"])
            self.assertGreater(cm.exception.retry_after, 0)
        self.assertEqual(len(self.sts.calls), before, "a throttled renewal must not reach STS")
        self.assertIsNotNone(self.row("sid-early"), "throttling must not touch the grant")

    def test_two_clients_of_one_session_never_refuse_each_other(self):
        """Two invocations share one grant row. B renewing (or re-minting)
        moves the row's clocks; A, whose own credential is about to expire,
        must then be told to retry, not refused for good (judge, !109)."""
        a = self.svc.mint_agentcore("sid-shared", "alice", SPEC, ttl_s=HOUR)
        b = self.svc.mint_agentcore("sid-shared", "alice", SPEC, ttl_s=HOUR)
        self.assertEqual(a["renew"]["token"], b["renew"]["token"])
        self.due("sid-shared")
        self.assertIsNotNone(self.svc.renew_agentcore("sid-shared", b["renew"]["token"]), "B renews")
        with self.assertRaises(self.mod.RenewThrottled):
            self.svc.renew_agentcore("sid-shared", a["renew"]["token"])
        self.due("sid-shared")
        self.assertIsNotNone(self.svc.renew_agentcore("sid-shared", a["renew"]["token"]), "A renews after the interval")

    def test_concurrent_renewals_of_one_session_cost_one_sts_call(self):
        g = self.svc.mint_agentcore("sid-burst", "alice", SPEC, ttl_s=HOUR)
        self.due("sid-burst")
        before = len(self.sts.calls)
        ok, throttled = [], []
        def renew():
            try:
                ok.append(self.svc.renew_agentcore("sid-burst", g["renew"]["token"]))
            except self.mod.RenewThrottled:
                throttled.append(1)
        threads = [threading.Thread(target=renew) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(self.sts.calls) - before, len([x for x in ok if x]))
        self.assertLessEqual(len(self.sts.calls) - before, 1, "one renewal slot per interval")
        self.assertEqual(len(ok) + len(throttled), 8)

    def test_the_workbench_refresh_is_throttled_by_the_same_slot(self):
        self.svc.mint_agentcore("sid-wb-slot", "alice", SPEC, ttl_s=HOUR)
        self.assertIsNotNone(self.svc.rotate("sid-wb-slot"))
        before = len(self.sts.calls)
        for _ in range(5):
            self.assertIsNone(self.svc.rotate("sid-wb-slot"))
        self.assertEqual(len(self.sts.calls), before, "the workbench refresh cannot loop STS either")
        self.assertIsNotNone(self.row("sid-wb-slot"))

    def test_renewal_routes_from_the_row_not_the_request(self):
        g = self.svc.mint_agentcore("sid-route", "alice", SPEC, ttl_s=HOUR)
        self.due("sid-route")
        r = self.svc.renew_agentcore("sid-route", g["renew"]["token"])
        self.assertEqual(r["endpoint"], SPEC["base_url"])
        self.assertEqual(self.sts.calls[-1]["RoleSessionName"], "sid-route")
        self.assertIn("gateway/gw1", self.sts.calls[-1]["Policy"])

    # ------------------------------------------------------------ concurrency

    def test_repeated_mints_on_one_session_share_a_token(self):
        """Every invocation on a warm session mints; the nonce must survive
        each re-mint, or a second invocation would end the first one's
        renewals. Sequential on purpose: per-item atomicity of concurrent
        UpdateItem calls is DynamoDB's guarantee, which moto does not model
        faithfully under threads (a spurious ConditionalCheckFailed on CI)."""
        grants = [self.svc.mint_agentcore("sid-race", "alice", SPEC, ttl_s=HOUR) for _ in range(8)]
        self.assertTrue(all(grants))
        tokens = {g["renew"]["token"] for g in grants}
        self.assertEqual(len(tokens), 1, "a second invocation must not invalidate the first one's renewals")
        self.due("sid-race")
        self.assertIsNotNone(self.svc.renew_agentcore("sid-race", tokens.pop()))

    def test_another_user_cannot_take_over_a_session(self):
        self.svc.mint_agentcore("sid-own", "alice", SPEC, ttl_s=HOUR)
        self.assertIsNone(self.svc.mint_agentcore("sid-own", "mallory", SPEC, ttl_s=HOUR))
        self.assertEqual(self.row("sid-own")["user"], "alice")


class StopFailsLoudTests(unittest.TestCase):
    """A stop that answered 200 while the grant survived would tell the user a
    credential is dead when its renewals still work."""

    def test_stop_and_delete_refuse_when_revocation_fails(self):
        api = importlib.import_module("app.api.sessions")
        item = {"session_id": "s1", "runtime_session_id": "rsid-x", "name": "n", "status": "running"}
        for call in (api.stop_session, api.delete_session):
            with patch.object(api, "session_service") as sessions, \
                    patch.object(api, "llm_credentials_service") as llm, \
                    patch.object(api, "workspace_credentials_service") as ws, \
                    patch.object(api, "_end_sandboxes") as sandboxes, \
                    patch.object(api, "_end_compute") as compute:
                sessions.get_session.return_value = dict(item)
                llm.revoke.side_effect = RuntimeError("throttled")
                with self.assertRaises(HTTPException) as cm:
                    call("s1", user="alice")
                self.assertEqual(cm.exception.status_code, 503)
                sessions.set_status.assert_not_called()  # still reported live (Security Agent, !109)
                ws.revoke.assert_not_called()
                sandboxes.assert_not_called()
                compute.assert_not_called()


class RenewRouteTests(unittest.TestCase):
    """The route through FastAPI, not just the service: hostile bodies get
    the one 401, a throttle gets 429 + Retry-After, and nothing in the answer
    tells a refused caller why (judge, !109)."""

    def setUp(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        self.api = importlib.import_module("app.api.llm_credentials")
        self.svc_mod = importlib.import_module("app.services.llm_credentials_service")
        app = FastAPI()
        app.include_router(self.api.router)
        self.client = TestClient(app)

    def post(self, body, **kw):
        return self.client.post("/api/v1/llm-credentials/renew", json=body, **kw)

    def test_hostile_bodies_all_get_the_same_401(self):
        with patch.object(self.api, "llm_credentials_service") as llm:
            llm.renew_agentcore.return_value = None
            bodies = [{}, {"runtime_session_id": "x"}, {"token": "y"},
                      {"runtime_session_id": "x", "token": "y"},
                      {"runtime_session_id": ["x"], "token": {"a": 1}},
                      {"runtime_session_id": "x" * 10_000, "token": "y"}]
            answers = {(r.status_code, r.text) for r in (self.post(b) for b in bodies)}
        self.assertEqual(answers, {(401, '{"detail":"Renewal refused"}')})

    def test_a_non_json_body_is_rejected_without_reaching_the_service(self):
        with patch.object(self.api, "llm_credentials_service") as llm:
            r = self.client.post("/api/v1/llm-credentials/renew", content=b"not json",
                                 headers={"Content-Type": "application/json"})
            llm.renew_agentcore.assert_not_called()
        self.assertEqual(r.status_code, 422)

    def test_a_throttle_is_429_with_retry_after(self):
        with patch.object(self.api, "llm_credentials_service") as llm:
            llm.renew_agentcore.side_effect = self.svc_mod.RenewThrottled(37)
            r = self.post({"runtime_session_id": "x", "token": "y"})
        self.assertEqual(r.status_code, 429)
        self.assertEqual(r.headers.get("retry-after"), "37")

    def test_success_returns_only_the_credential_block(self):
        with patch.object(self.api, "llm_credentials_service") as llm:
            llm.renew_agentcore.return_value = {"access_key_id": "A"}
            r = self.post({"runtime_session_id": "x", "token": "y"})
        self.assertEqual((r.status_code, r.json()), (200, {"llm_credentials": {"access_key_id": "A"}}))

    def test_the_route_is_mounted_on_the_management_app_only(self):
        """Mounted beside sessions.router (the portal deployment the kernel
        reaches through CloudFront), not on the entry-only data plane, and
        with no user dependency: the caller is a kernel."""
        route = next(r for r in self.api.router.routes if r.path == "/api/v1/llm-credentials/renew")
        self.assertEqual(route.methods, {"POST"})
        self.assertEqual(route.dependant.dependencies, [])
        main_src = (Path(__file__).resolve().parents[1] / "app" / "main.py").read_text()
        entry_only, _, management = main_src.partition("else:")
        self.assertNotIn("llm_credentials.router", entry_only)
        self.assertIn("app.include_router(llm_credentials.router)", management)

if __name__ == "__main__":
    unittest.main()
