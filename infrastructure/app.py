#!/usr/bin/env python3
"""CDK app for the agent platform sample.

Deploy order:
  1. NetworkStack + PlatformStack
  2. push kernel/backend images (scripts/build-and-push.sh)
  3. RuntimeStack
  4. PortalStack (optional — backend/frontend can also run locally)
"""

import os

import aws_cdk as cdk

from stacks.network_stack import NetworkStack
from stacks.platform_stack import PlatformStack
from stacks.portal_stack import PortalStack
from stacks.runtime_stack import RuntimeStack

app = cdk.App()

env = cdk.Environment(
    account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
    region=os.environ.get("CDK_DEFAULT_REGION"),
)

network = NetworkStack(app, "AgentPlatformNetwork", env=env)
platform = PlatformStack(app, "AgentPlatformPlatform", env=env)
runtime = RuntimeStack(
    app, "AgentPlatformRuntime", network=network, platform=platform, env=env
)
PortalStack(
    app,
    "AgentPlatformPortal",
    network=network,
    platform=platform,
    runtime=runtime,
    env=env,
)

app.synth()
