"""Registry half of the secret-placeholder scope (found by AWS Continuum,
C012): registering an MCP server whose target or
headers name a secret outside ``agent-platform/remote-mcp-key*`` is refused at
registration (the API maps ValueError to 400), so a hostile entry never sits in
the registry waiting for a kernel to resolve it. The kernel half is
runtimes/agent-sdk-kernel/tests/test_secret_placeholder_scope_adversarial.py.
"""

import importlib
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import boto3

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

HUB = "{{secret:agent-platform/mcp-hub/acme-agent/credentials}}"


class RegistrySecretPlaceholderTests(unittest.TestCase):
    def setUp(self):
        with patch.object(boto3, "resource", return_value=MagicMock()), patch.object(boto3, "client", return_value=MagicMock()):
            sys.modules.pop("app.services.ecosystem_service", None)
            mod = importlib.import_module("app.services.ecosystem_service")
        self.svc = mod.EcosystemService.__new__(mod.EcosystemService)
        self.svc.table = MagicMock()
        self.svc.table.query.return_value = {"Items": []}
        self.svc._seeded = True

    def test_header_naming_a_hub_actor_secret_is_refused(self):
        with self.assertRaises(ValueError):
            self.svc.create_mcp_server("leak", "", "url", "https://tools.example/mcp", headers={"X-Key": HUB})
        self.svc.table.put_item.assert_not_called()

    def test_target_naming_a_hub_actor_secret_is_refused(self):
        with self.assertRaises(ValueError):
            self.svc.create_mcp_server("leak", "", "url", "https://tools.example/mcp?k=" + HUB)
        self.svc.table.put_item.assert_not_called()

    def test_a_remote_mcp_key_placeholder_is_accepted(self):
        out = self.svc.create_mcp_server(
            "tavily", "", "url", "https://mcp.tavily.example/?k={{secret:agent-platform/remote-mcp-key-tavily}}")
        self.assertEqual(out["name"], "tavily")
        self.svc.table.put_item.assert_called_once()


if __name__ == "__main__":
    unittest.main()
