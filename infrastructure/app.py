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
from stacks.team_auth_stack import TeamAuthStack
from stacks.team_demo_stack import TeamDemoStack

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

# Optional: enterprise-SSO auth chain demo (Keycloak IdP + team APIs, then a
# JWT-inbound runtime). Deploy TeamAuth first, push its images, verify
# Keycloak is up, then deploy TeamDemo and run scripts/deploy_team_gateway.py.
team_auth = TeamAuthStack(
    app, "AgentPlatformTeamAuth", network=network, platform=platform, env=env
)
TeamDemoStack(
    app,
    "AgentPlatformTeamDemo",
    network=network,
    platform=platform,
    runtime=runtime,
    team_auth=team_auth,
    env=env,
)

app.synth()
