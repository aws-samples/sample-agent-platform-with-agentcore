"""``GET /api/v1/team-demo/config`` must not hand out the address of the
JWT-inbound runtime (found by AWS Continuum, C004).

The team-demo runtime admits any portal login token, so its ARN is all a
signed-in user needs to invoke it directly with a payload of their own. The
kernel restricts such payloads (KERNEL_INBOUND=jwt, see
runtimes/agent-sdk-kernel/tests/test_jwt_inbound_payload_adversarial.py);
this route is the defence in depth: nothing a caller needs to use the demo
includes the runtime ARN, the gateway id or URL, or the issuer, since
``/invoke`` reads them server-side.
"""

import importlib
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

CFG = {
    "runtime_arn": "arn:aws:bedrock-agentcore:ap-northeast-1:ACCOUNTID001:runtime/team_demo_kernel-XXXX",
    "mcp_url": "https://agent-platform-team-abc.gateway.bedrock-agentcore.ap-northeast-1.amazonaws.com/mcp",
    "gateway_id": "agent-platform-team-abc",
    "issuer": "https://sso.example/realms/agent-platform",
    "teams": ["team-a", "team-b", "team-c"],
}


class TeamDemoConfigTests(unittest.TestCase):
    def setUp(self):
        self.mod = importlib.import_module("app.api.team_demo")

    def config(self, cfg):
        with patch.object(self.mod, "_demo_config", return_value=cfg):
            return self.mod.team_demo_config(user="mallory")

    def test_runtime_and_gateway_addresses_are_not_returned(self):
        out = self.config(CFG)
        for key in ("runtime_arn", "mcp_url", "gateway_id", "issuer"):
            self.assertNotIn(key, out)
        text = repr(out)
        for value in (CFG["runtime_arn"], CFG["mcp_url"], CFG["gateway_id"], "team_demo_kernel"):
            self.assertNotIn(value, text)

    def test_what_the_demo_needs_is_still_there(self):
        out = self.config(CFG)
        self.assertIs(out["enabled"], True)
        self.assertEqual(out["teams"], CFG["teams"])
        self.assertIn("auth_mode_oidc", out)

    def test_not_deployed_is_reported_disabled(self):
        self.assertIs(self.config({})["enabled"], False)
        self.assertIs(self.config({"runtime_arn": CFG["runtime_arn"]})["enabled"], False)


if __name__ == "__main__":
    unittest.main()
