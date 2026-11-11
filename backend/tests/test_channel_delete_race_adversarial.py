"""Deleting a channel while a webhook call is in flight
must not leave a tokenless channel that admits anyone who knows its id.

The race: handle_webhook verifies the token, routes the call, then bumps the
message counter. If an admin deletes the channel during the call, the counter
update runs against a missing item. DynamoDB's update_item creates the item
when it does not exist, so the row comes back with only the counter fields:
no token, no kind. The next request with an empty token then compares "" to ""
and is admitted.

The race window is simulated by deleting the channel inside the routed call.
DynamoDB is moto, so update_item has the real upsert semantics. Found by
AWS Continuum (C017).
"""

import importlib
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import boto3
from moto import mock_aws

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

TABLE = "agent-platform"


class ChannelResurrectionTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {
            "AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing",
            "AWS_DEFAULT_REGION": "us-east-1",
        })
        self.env.start()
        self.mock = mock_aws()
        self.mock.start()
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName=TABLE,
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"},
                       {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"},
                                  {"AttributeName": "SK", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        mod = importlib.import_module("app.services.channel_service")
        self.svc = mod.ChannelService.__new__(mod.ChannelService)
        self.svc.table = ddb.Table(TABLE)
        channel = self.svc.create_channel(user="admin", name="ops", target="agent-sdk")
        self.channel_id, self.token = channel["id"], channel["token"]

    def tearDown(self):
        self.mock.stop()
        self.env.stop()

    def _delete_during_call(self, *args, **kwargs):
        self.svc.delete_channel(self.channel_id)
        return {"ok": True, "result": "in-flight answer"}

    def test_delete_during_an_in_flight_call_does_not_resurrect_a_tokenless_channel(self):
        with patch("app.services.invocation_service.invoke", side_effect=self._delete_during_call):
            try:
                self.svc.handle_webhook(self.channel_id, self.token, "hello")
            except Exception:
                pass  # failing the in-flight call is an acceptable outcome

        routed = []
        with patch("app.services.invocation_service.invoke",
                   side_effect=lambda **kw: routed.append(kw) or {"ok": True}):
            with self.assertRaises((KeyError, PermissionError, ValueError)):
                self.svc.handle_webhook(self.channel_id, "", "no token")
        self.assertEqual(routed, [], "a request without a token was routed to a kernel")

    def test_a_channel_row_without_a_token_is_never_admitted(self):
        # a row missing its credential (however it got there) fails closed
        self.svc.table.put_item(Item={"PK": "CHANNEL", "SK": "CH#bare", "message_count": 1})
        with patch("app.services.invocation_service.invoke", return_value={"ok": True}) as invoke:
            with self.assertRaises((KeyError, PermissionError, ValueError)):
                self.svc.handle_webhook("bare", "", "no token")
        invoke.assert_not_called()

    def test_the_valid_token_still_works(self):
        with patch("app.services.invocation_service.invoke", return_value={"ok": True}) as invoke:
            self.assertEqual(self.svc.handle_webhook(self.channel_id, self.token, "hi"), {"ok": True})
        invoke.assert_called_once()

    def test_a_channel_created_before_kind_existed_still_works(self):
        """Shape of the four live token channels without a kind attribute
        (production, 2026-11-06): requiring an explicit kind would cut them
        off on merge. The stored token is what authenticates."""
        self.svc.table.put_item(Item={
            "PK": "CHANNEL", "SK": "CH#legacy", "name": "legacy", "enabled": True,
            "target": "agent-sdk", "token": "legacy-token-value", "message_count": 0,
            "created_by": "admin", "created_at": "2026-07-21T04:45:57+00:00",
        })
        with patch("app.services.invocation_service.invoke", return_value={"ok": True}) as invoke:
            self.assertEqual(self.svc.handle_webhook("legacy", "legacy-token-value", "hi"), {"ok": True})
            for bad in ("", "wrong", None):
                with self.assertRaises(PermissionError):
                    self.svc.handle_webhook("legacy", bad, "hi")
        invoke.assert_called_once()

    def test_the_live_tombstone_shape_is_refused(self):
        """Exact shape of a resurrected row found in production (2026-11-06):
        only the counter fields survive."""
        self.svc.table.put_item(Item={
            "PK": "CHANNEL", "SK": "CH#tomb", "message_count": 1,
            "last_message_at": "2026-08-03T14:46:52.538289+00:00",
        })
        with patch("app.services.invocation_service.invoke", return_value={"ok": True}) as invoke:
            for tok in ("", None, "anything"):
                with self.assertRaises((KeyError, PermissionError, ValueError)):
                    self.svc.handle_webhook("tomb", tok, "no token")
        invoke.assert_not_called()


if __name__ == "__main__":
    unittest.main()
