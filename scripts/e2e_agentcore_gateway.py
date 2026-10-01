#!/usr/bin/env python3
"""End-to-end test for the agentcore_gateway model backend, against real AWS.

Unlike the moto-based suites this one provisions a live AgentCore Gateway and
drives the *real* Claude Code CLI through the *real* kernel shim, so the whole
authentication chain is exercised as deployed:

    Claude Code subprocess          per-invocation loopback token
        -> llm_shim (kernel)        SigV4 over per-session STS credentials
        -> AgentCore Gateway        IAM authorizer + session policy
        -> LiteLLM /v1/messages     API key injected from the token vault
        -> the upstream model LiteLLM routes to

Nothing in the chain is stubbed. The only simulation is the microVM itself:
the shim runs in this process instead of inside AgentCore Runtime, which is
exactly where it runs in production (in the kernel process), so the code path
is identical.

Asserts:

  [chain]     Claude Code completes a turn through the shim, and the shim
              observed the request — i.e. the CLI really used the gateway and
              not some ambient credential.
  [protocol]  which paths Claude Code actually sends (reported, and asserted to
              be a subset of what AgentCore Gateway accepts inbound).
  [scoping]   the session policy narrows the credential to one gateway: the
              same credential is refused on a second gateway.
  [failfast]  the shim refuses a model this session was not routed to (an
              optimisation, not a boundary — see mint_agentcore's docstring).
  [revoke]    llm_credentials_service.revoke() stops this session's
              credentials while a second live session keeps working.

Usage:  python3 scripts/e2e_agentcore_gateway.py [--region us-east-1] [--keep]

Configuration is by environment: LITELLM_ENDPOINT and
LITELLM_CREDENTIAL_PROVIDER_ARN are required; LITELLM_ALLOWED_MODELS,
LITELLM_CHAIN_MODEL and LITELLM_PATHS have defaults. For a LiteLLM that is not reachable from
the internet, set LITELLM_ROUTING_DOMAIN (an internal ALB's DNS name),
LITELLM_VPC_ID, LITELLM_SUBNET_IDS and LITELLM_LATTICE_SG_IDS: the target then
gets a managed VPC Lattice private endpoint and the gateway never leaves AWS's
network on the way to LiteLLM.

Requires: credentials able to create IAM roles, an AgentCore Gateway and a
DynamoDB table, plus the `claude` CLI on PATH. Everything it creates is torn
down unless --keep is passed.
"""
import argparse
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

import boto3
from botocore.exceptions import ClientError

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAIL: list[str] = []
SUFFIX = secrets.token_hex(4)

# The gateway target routes to this LiteLLM deployment. Overridable so a fork
# can point at a staging instance, but the default is the one Prod-LiteLLM
# already runs on this account.
# The gateway target routes to this LiteLLM deployment, and this API-key
# credential provider holds the LiteLLM virtual key. Both are required. The
# provider is created out of band (AWS Console: AgentCore -> Identity -> API
# key credential providers) so a live key never lands in this script or its
# logs; the e2e's job is to prove the provider ARN and the target wiring work
# end to end.
LITELLM_ENDPOINT = os.environ.get("LITELLM_ENDPOINT", "")
LITELLM_CREDENTIAL_PROVIDER_ARN = os.environ.get("LITELLM_CREDENTIAL_PROVIDER_ARN", "")

# The list of models the virtual key permits. The gateway target refuses any
# model outside this set at its own layer, and LiteLLM refuses it at the key
# layer — either check is enough on its own, both layers are here on purpose
# so a misconfiguration surfaces immediately rather than at the far end.
LITELLM_ALLOWED_MODELS = [
    m.strip()
    for m in os.environ.get(
        "LITELLM_ALLOWED_MODELS",
        "claude-opus-5-5,claude-sonnet-5-5,gpt-6-astra,kimi-k3",
    ).split(",")
    if m.strip()
]

# Private path to LiteLLM. When LITELLM_ROUTING_DOMAIN is set the target gets a
# managed VPC Lattice private endpoint: AgentCore places resource-gateway ENIs
# in these subnets and connects to the routing domain (an internal ALB), while
# TLS SNI / Host stay on LITELLM_ENDPOINT's name so the ALB's public cert
# matches. Unset, the target dials LITELLM_ENDPOINT over the internet.
LITELLM_ROUTING_DOMAIN = os.environ.get("LITELLM_ROUTING_DOMAIN", "")
LITELLM_VPC_ID = os.environ.get("LITELLM_VPC_ID", "")
LITELLM_SUBNET_IDS = [
    s.strip() for s in os.environ.get("LITELLM_SUBNET_IDS", "").split(",") if s.strip()
]
LITELLM_LATTICE_SG_IDS = [
    s.strip() for s in os.environ.get("LITELLM_LATTICE_SG_IDS", "").split(",") if s.strip()
]

# Inbound paths the target accepts. The gateway does no protocol translation:
# it matches path + model, swaps in the API key and relays the body (and the
# SSE stream) verbatim, so whether a given model works on a given path is
# LiteLLM's call. Every allowed model is declared on every path here and the
# [protocol-matrix] probe reports what LiteLLM actually serves.
LITELLM_PATHS = [
    p.strip()
    for p in os.environ.get(
        "LITELLM_PATHS", "/v1/messages,/v1/chat/completions,/v1/responses"
    ).split(",")
    if p.strip()
]

# The one used for the [chain] / multi-turn / revoke / failfast steps, which
# drive the real Claude Code CLI and therefore need an Anthropic-native model.
LITELLM_CHAIN_MODEL = os.environ.get("LITELLM_CHAIN_MODEL", "claude-sonnet-5-5")

# Anything in the allowed set but not the chain model gets exercised by the
# lightweight [multimodel] smoke: one /v1/messages "hi" per model, no CLI, no
# tools. That is where any Anthropic-to-OpenAI protocol translation LiteLLM
# does for gpt-* / kimi-* is stressed.
LITELLM_SMOKE_MODELS = [
    m for m in LITELLM_ALLOWED_MODELS if m != LITELLM_CHAIN_MODEL
]

# No REQUEST interceptor. Claude Code sends first-party-only fields
# (context_management today) that LiteLLM's Bedrock adapter rejects; the
# LiteLLM model entries carry `drop_params: true` so LiteLLM strips them. An
# interceptor Lambda could do the same at the gateway, but every model call
# then pays a synchronous Lambda hop and the request body is capped by
# Lambda's 6 MB invoke payload (about 4.5 MB of JSON after base64), which a
# long Claude Code session can reach.


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + (f"  ({detail})" if detail else ""))
    if not ok:
        FAIL.append(label)


def step(msg: str) -> None:
    print(f"\n=== {msg}")


# --------------------------------------------------------------- provisioning


class Fixture:
    """Live AWS resources for one run."""

    def __init__(self, region: str) -> None:
        self.region = region
        self.iam = boto3.client("iam")
        self.sts = boto3.client("sts")
        self.ddb = boto3.client("dynamodb", region_name=region)
        self.acc = boto3.client("bedrock-agentcore-control", region_name=region)
        self.account = self.sts.get_caller_identity()["Account"]
        self.gw_role = f"e2e-acgw-exec-{SUFFIX}"
        self.caller_role = f"e2e-acgw-caller-{SUFFIX}"
        self.table = f"e2e-acgw-{SUFFIX}"
        self.gateways: list[str] = []

    # -- helpers ----------------------------------------------------------

    def _caller_principal(self) -> str:
        """The role this script runs as, so the caller role can trust it.

        In production the trusting principal is the backend's IRSA role; here
        it is whatever identity is running the test. SSO reserved roles
        (``arn:aws:iam::<acct>:role/AWSReservedSSO_*``) cannot be used as an
        AssumeRole *Principal* — IAM refuses the trust policy with
        MalformedPolicyDocument — so for those we widen to the account root.
        That is safe here because the role lives for the length of one run.
        """
        arn = self.sts.get_caller_identity()["Arn"]
        if ":assumed-role/" in arn:
            name = arn.split(":assumed-role/")[1].split("/")[0]
            if name.startswith("AWSReservedSSO_"):
                return f"arn:aws:iam::{self.account}:root"
            return f"arn:aws:iam::{self.account}:role/{name}"
        return arn

    def _gw_role_policy(self) -> dict:
        """Policy for the gateway execution role.

        The one capability needed is retrieving the outbound API key (the
        LiteLLM virtual key) from AgentCore Identity's token vault during a
        request; without the GetResourceApiKey / secretsmanager
        grants the gateway returns 400 "Failed to fetch outbound api key.
        Failed to get workload identity token" and every call — chain,
        scoping, revoke — surfaces the same error.
        """
        vault = f"arn:aws:bedrock-agentcore:{self.region}:{self.account}"
        provider_name = LITELLM_CREDENTIAL_PROVIDER_ARN.rsplit("/", 1)[-1]
        return {
            "Version": "2012-10-17",
            "Statement": [
                {
                    # Two actions, one resource set. GetWorkloadAccessToken
                    # is the first hop the gateway makes: it exchanges its
                    # execution role for a workload-scoped token, which it
                    # then presents to GetResourceApiKey to unwrap the
                    # LiteLLM key. Without the first action the docs' policy
                    # is not sufficient — this is the exact failure mode we
                    # observed as "Failed to get workload identity token"
                    # despite GetResourceApiKey being granted.
                    "Sid": "AgentCoreApiKeyTokenVaultDefault",
                    "Effect": "Allow",
                    "Action": [
                        "bedrock-agentcore:GetWorkloadAccessToken",
                        "bedrock-agentcore:GetResourceApiKey",
                    ],
                    "Resource": [
                        f"{vault}:token-vault/default",
                        f"{vault}:workload-identity-directory/default",
                        f"{vault}:workload-identity-directory/default/"
                        "workload-identity/*",
                    ],
                },
                {
                    "Sid": "AgentCoreApiKeyTokenVaultPerKey",
                    "Effect": "Allow",
                    "Action": "bedrock-agentcore:GetResourceApiKey",
                    "Resource": LITELLM_CREDENTIAL_PROVIDER_ARN,
                },
                {
                    "Sid": "AgentCoreApiKeySecret",
                    "Effect": "Allow",
                    "Action": "secretsmanager:GetSecretValue",
                    "Resource": (
                        f"arn:aws:secretsmanager:{self.region}:"
                        f"{self.account}:secret:"
                        f"bedrock-agentcore-identity!default/apikey/"
                        f"{provider_name}-*"
                    ),
                },
            ],
        }

    def _role(self, name: str, trust: dict, policy: dict, max_session: int = 3600) -> str:
        try:
            arn = self.iam.create_role(
                RoleName=name,
                AssumeRolePolicyDocument=json.dumps(trust),
                MaxSessionDuration=max_session,
                Description="temporary: e2e_agentcore_gateway",
            )["Role"]["Arn"]
        except ClientError as e:
            if e.response["Error"]["Code"] != "EntityAlreadyExists":
                raise
            arn = self.iam.get_role(RoleName=name)["Role"]["Arn"]
        self.iam.put_role_policy(
            RoleName=name, PolicyName="e2e", PolicyDocument=json.dumps(policy)
        )
        return arn

    # -- create -----------------------------------------------------------

    def create(self) -> None:
        step("provisioning live AWS resources")
        self.ddb.create_table(
            TableName=self.table,
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        self.ddb.get_waiter("table_exists").wait(TableName=self.table)
        print(f"  table  {self.table}")

        gw_trust = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
                    "Action": "sts:AssumeRole",
                    "Condition": {"StringEquals": {"aws:SourceAccount": self.account}},
                }
            ],
        }
        # The gateway execution role must reach AgentCore Identity's token
        # vault so the LiteLLM API key can be fetched and injected into
        # outbound requests.
        gw_policy = self._gw_role_policy()
        gw_arn_role = self._role(self.gw_role, gw_trust, gw_policy)
        print(f"  role   {self.gw_role} (gateway execution)")

        # Two gateways: the session credential is scoped to the first, and the
        # second exists only to prove the scoping actually bites.
        for label in ("primary", "decoy"):
            g = self.acc.create_gateway(
                name=f"e2e-{label}-{SUFFIX}",
                roleArn=gw_arn_role,
                protocolType="MCP",
                authorizerType="AWS_IAM",
                exceptionLevel="DEBUG",
                description="temporary: e2e_agentcore_gateway",
            )
            self.gateways.append(g["gatewayId"])
            print(f"  gateway {g['gatewayId']} ({label})")
        for gid in self.gateways:
            for _ in range(40):
                if self.acc.get_gateway(gatewayIdentifier=gid)["status"] != "CREATING":
                    break
                time.sleep(4)
        # IAM propagation before the gateway's first outbound.
        time.sleep(15)
        for gid in self.gateways:
            t = self.acc.create_gateway_target(
                gatewayIdentifier=gid,
                name="litellm",
                # An inference *provider* target rather than the built-in
                # bedrock-mantle connector: the gateway forwards Anthropic
                # Messages requests to our LiteLLM deployment, and LiteLLM
                # keeps its multi-provider routing (Bedrock Anthropic today,
                # OpenAI / Gemini tomorrow) exactly as it does for every other
                # client. What this backend gives up in return is the
                # llm-edge pod — one fewer service on the platform side, no
                # provider key living anywhere in our EKS.
                targetConfiguration={
                    "inference": {
                        "provider": {
                            "endpoint": LITELLM_ENDPOINT,
                            "operations": [
                                {
                                    "path": path,
                                    # The models named here are what a caller
                                    # is *allowed* to route to; the shim
                                    # additionally fail-fasts against the
                                    # per-session allowlist minted by the
                                    # backend.
                                    "models": [
                                        {"model": m}
                                        for m in LITELLM_ALLOWED_MODELS
                                    ],
                                }
                                for path in LITELLM_PATHS
                            ],
                        }
                    }
                },
                # An API_KEY provider held in AgentCore Identity's token vault
                # substitutes the caller's Authorization header on the way
                # out, so a container never sees the LiteLLM key. The
                # provider is created out of band (Console) and referenced by
                # ARN; see LITELLM_CREDENTIAL_PROVIDER_ARN at the top.
                credentialProviderConfigurations=[
                    {
                        "credentialProviderType": "API_KEY",
                        "credentialProvider": {
                            "apiKeyCredentialProvider": {
                                "providerArn": LITELLM_CREDENTIAL_PROVIDER_ARN,
                                "credentialLocation": "HEADER",
                                "credentialParameterName": "Authorization",
                                # No trailing space: the gateway inserts the
                                # separator itself. "Bearer " goes out as
                                # "Bearer  <key>" and LiteLLM rejects it.
                                "credentialPrefix": "Bearer",
                            }
                        },
                    }
                ],
                # Curating headers here is the AgentCore-side counterpart of
                # llm-edge's FORWARD_HEADERS allowlist, and it is not optional:
                # left unset the gateway relays whatever the caller sent,
                # including the caller's own x-amz-security-token. LiteLLM
                # accepts anthropic-beta (so Claude Code's prompt-caching
                # advertisement passes through), unlike the bedrock-mantle
                # connector, which is the one payoff of not routing through
                # that connector. content-type has to be listed explicitly:
                # once allowedRequestHeaders is set, a connector-style target
                # stops relaying it.
                metadataConfiguration={
                    "allowedRequestHeaders": [
                        "content-type",
                        "anthropic-version",
                        "anthropic-beta",
                        "accept",
                    ]
                },
                **self._private_endpoint(),
            )
            for _ in range(90):
                st = self.acc.get_gateway_target(
                    gatewayIdentifier=gid, targetId=t["targetId"]
                )
                if st["status"] != "CREATING":
                    break
                time.sleep(4)
            print(f"  target  {gid} -> {st['status']} {st.get('statusReasons', '')}")
            for r in st.get("privateEndpointManagedResources") or []:
                print(f"          private endpoint {r.get('domain')} via {r.get('resourceGatewayArn')}")
            if st["status"] != "READY":
                raise SystemExit("inference target did not become READY")

        self.caller_arn = self._role(
            self.caller_role,
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Principal": {"AWS": self._caller_principal()},
                        "Action": ["sts:AssumeRole", "sts:TagSession"],
                    }
                ],
            },
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": "bedrock-agentcore:InvokeGateway",
                        "Resource": [
                            f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:gateway/{g}"
                            for g in self.gateways
                        ],
                    }
                ],
            },
        )
        print(f"  role   {self.caller_role} (per-session caller)")
        # The role policy deliberately allows BOTH gateways; the narrowing to
        # one is the session policy the service attaches at mint time, so the
        # scoping assertion tests the session policy and not this role.
        time.sleep(12)

    def _private_endpoint(self) -> dict:
        """Top-level privateEndpoint kwarg for create_gateway_target, if any.

        It sits beside targetConfiguration, not inside inference.provider.
        The first target creation in an account also creates the
        AWSServiceRoleForBedrockAgentCoreGatewayNetwork service-linked role.
        """
        if not LITELLM_ROUTING_DOMAIN:
            return {}
        mvr = {
            "vpcIdentifier": LITELLM_VPC_ID,
            "subnetIds": LITELLM_SUBNET_IDS,
            "endpointIpAddressType": "IPV4",
            "routingDomain": LITELLM_ROUTING_DOMAIN,
        }
        if LITELLM_LATTICE_SG_IDS:
            mvr["securityGroupIds"] = LITELLM_LATTICE_SG_IDS
        return {"privateEndpoint": {"managedVpcResource": mvr}}

    def base_url(self, which: int = 0) -> str:
        gid = self.gateways[which]
        return (
            f"https://{gid}.gateway.bedrock-agentcore.{self.region}"
            f".amazonaws.com/inference"
        )

    # -- destroy ----------------------------------------------------------

    def destroy(self) -> None:
        step("tearing down")
        for gid in self.gateways:
            try:
                for t in self.acc.list_gateway_targets(gatewayIdentifier=gid).get(
                    "items", []
                ):
                    self.acc.delete_gateway_target(
                        gatewayIdentifier=gid, targetId=t["targetId"]
                    )
                # A target with a private endpoint takes tens of seconds to
                # release its Lattice association; the gateway refuses
                # deletion until the target is gone.
                for _ in range(40):
                    if not self.acc.list_gateway_targets(
                        gatewayIdentifier=gid
                    ).get("items"):
                        break
                    time.sleep(5)
                self.acc.delete_gateway(gatewayIdentifier=gid)
                print(f"  deleted gateway {gid}")
            except Exception as e:  # noqa: BLE001
                print(f"  gateway {gid}: {str(e)[:100]}")
        for name in (self.caller_role, self.gw_role):
            for pol in self.iam.list_role_policies(RoleName=name).get(
                "PolicyNames", []
            ):
                try:
                    self.iam.delete_role_policy(RoleName=name, PolicyName=pol)
                except Exception:  # noqa: BLE001
                    pass
            try:
                self.iam.delete_role(RoleName=name)
                print(f"  deleted role {name}")
            except Exception as e:  # noqa: BLE001
                print(f"  role {name}: {str(e)[:100]}")
        try:
            self.ddb.delete_table(TableName=self.table)
            print(f"  deleted table {self.table}")
        except Exception as e:  # noqa: BLE001
            print(f"  table: {str(e)[:100]}")


# ------------------------------------------------------------------ the test


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    ap.add_argument("--model", default=LITELLM_CHAIN_MODEL)
    ap.add_argument("--keep", action="store_true", help="leave AWS resources behind")
    args = ap.parse_args()

    if not (LITELLM_ENDPOINT and LITELLM_CREDENTIAL_PROVIDER_ARN):
        print("set LITELLM_ENDPOINT (https://...) and LITELLM_CREDENTIAL_PROVIDER_ARN")
        return 2

    if not any(
        os.access(os.path.join(p, "claude"), os.X_OK)
        for p in os.environ.get("PATH", "").split(os.pathsep)
        if p
    ):
        print("the `claude` CLI is not on PATH — cannot run the chain test")
        return 2

    fx = Fixture(args.region)
    fx.create()

    # ---- env must be set before app.config instantiates Settings() ----
    os.environ.update(
        PLATFORM_AWS_REGION=args.region,
        PLATFORM_DYNAMO_TABLE=fx.table,
        PLATFORM_AGENTCORE_GATEWAY_CALLER_ROLE_ARN=fx.caller_arn,
    )
    sys.path.insert(0, os.path.join(REPO, "backend"))
    sys.path.insert(0, os.path.join(REPO, "runtimes", "agent-sdk-kernel", "src"))
    from app.services.llm_credentials_service import LlmCredentialsService  # noqa: E402
    import llm_shim  # noqa: E402

    svc = LlmCredentialsService()

    # Observability only, so a chain assertion cannot be satisfied by Claude
    # Code quietly using some other credential. Both wrappers delegate to the
    # real implementation; nothing about the path under test changes.
    seen: list[str] = []
    _orig_proxy = llm_shim._Handler._proxy

    def _counting_proxy(self):  # noqa: ANN001, ANN202
        seen.append(f"{self.command} {self.path}")
        return _orig_proxy(self)

    # _sign_sigv4 is the one place the full request body is in hand, which is
    # what makes the shape of a multi-turn tool loop visible.
    sent: list[dict] = []
    _orig_sign = llm_shim._sign_sigv4

    def _recording_sign(grant, method, url, body):  # noqa: ANN001, ANN202
        try:
            b = json.loads(body) if body else {}
        except Exception:  # noqa: BLE001
            b = {}
        msgs = [m for m in (b.get("messages") or []) if isinstance(m, dict)]

        def _blocks(m: dict) -> list:
            c = m.get("content")
            return c if isinstance(c, list) else []

        sent.append(
            {
                "path": url.split("amazonaws.com", 1)[-1] or url,
                "bytes": len(body or b""),
                "model": b.get("model"),
                "msgs": len(msgs),
                "tools": len(b.get("tools") or []),
                "tool_result": sum(
                    1
                    for m in msgs
                    for c in _blocks(m)
                    if isinstance(c, dict) and c.get("type") == "tool_result"
                ),
                "stream": bool(b.get("stream")),
            }
        )
        return _orig_sign(grant, method, url, body)

    llm_shim._Handler._proxy = _counting_proxy
    llm_shim._sign_sigv4 = _recording_sign
    llm_shim.start()

    spec = {
        "backend": "agentcore_gateway",
        "base_url": fx.base_url(0),
        "model": args.model,
        "alias_models": {"haiku": args.model},
    }

    try:
        step("[chain] mint per-session credentials and run real Claude Code")
        sid_a = f"ses-{secrets.token_hex(20)}"
        grant = svc.mint_agentcore(sid_a, "alice", spec, team="team-alpha")
        check("mint_agentcore returned a grant", bool(grant))
        if not grant:
            return 1
        print(f"  mode={grant['mode']} region={grant['region']} "
              f"service={grant['service']} expires_at={grant['expires_at']}")
        print(f"  credential is STS: access_key_id={grant['access_key_id'][:8]}…, "
              f"session_token={'yes' if grant.get('session_token') else 'no'}")
        check(
            "grant carries no bearer token and no upstream key",
            "token" not in grant and "secret_name" not in grant,
        )

        local_token = llm_shim.register(grant)
        check("shim issued a per-invocation local token", bool(local_token))
        check("local token differs from the STS secret", local_token != grant["secret_access_key"])

        env = dict(os.environ)
        # Exactly what the kernel writes for a gateway-mode session.
        env.pop("CLAUDE_CODE_USE_BEDROCK", None)
        env.pop("CLAUDE_CODE_PROVIDER", None)
        env.pop("ANTHROPIC_DEFAULT_OPUS_MODEL", None)
        env.update(
            ANTHROPIC_BASE_URL=llm_shim.BASE_URL,
            ANTHROPIC_AUTH_TOKEN=local_token,
            ANTHROPIC_MODEL=args.model,
            ANTHROPIC_SMALL_FAST_MODEL=args.model,
            CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
            CLAUDE_CONFIG_DIR=os.path.join("/tmp", f"e2e-acgw-cfg-{SUFFIX}"),
        )
        before = len(seen)
        proc = subprocess.run(
            ["claude", "-p", "Reply with the result of 6*7 and nothing else."],
            env=env,
            capture_output=True,
            text=True,
            timeout=240,
            stdin=subprocess.DEVNULL,
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        print(f"  claude output: {out.strip()[:160]!r}")
        check("claude produced no API error", "API Error" not in out, out.strip()[:90])
        check("claude answered", "42" in out)
        check(
            "the shim forwarded Claude Code's request(s)",
            len(seen) > before,
            f"{len(seen) - before} request(s)",
        )
        print(f"  paths Claude Code sent through the chain: {seen[before:]}")
        check(
            "every path is one AgentCore Gateway accepts inbound",
            all(
                p.split(" ", 1)[1].split("?")[0]
                in ("/v1/messages", "/v1/chat/completions", "/v1/responses", "/v1/models")
                for p in seen[before:]
            ),
            ",".join(sorted({p.split(" ", 1)[1] for p in seen[before:]})),
        )

        step("[task] a real multi-turn task with tool use, not a single turn")
        work = os.path.join("/tmp", f"e2e-acgw-work-{SUFFIX}")
        os.makedirs(work, exist_ok=True)
        before_n, before_sent = len(seen), len(sent)
        proc = subprocess.run(
            [
                "claude",
                "-p",
                "Create fizz.py that prints the FizzBuzz sequence for 1 to 15, "
                "one value per line. Run it with python3. Then reply with only "
                "the 15th line of its output.",
                "--allowedTools",
                "Write,Read,Bash",
            ],
            env=env,
            cwd=work,
            capture_output=True,
            text=True,
            timeout=600,
            stdin=subprocess.DEVNULL,
        )
        tout = (proc.stdout or "") + (proc.stderr or "")
        print(f"  claude output: {tout.strip()[:200]!r}")
        rounds = sent[before_sent:]
        print(f"  {len(seen) - before_n} request(s) through the shim:")
        print(f"    {'#':>2}  {'path':26} {'bytes':>7} {'msgs':>5} {'tools':>5}"
              f" {'tool_result':>11} {'stream':>6}")
        for i, r in enumerate(rounds, 1):
            print(
                f"    {i:>2}  {r['path'][:26]:26} {r['bytes']:>7} {r['msgs']:>5}"
                f" {r['tools']:>5} {r['tool_result']:>11} {str(r['stream']):>6}"
            )
        check("task produced no API error", "API Error" not in tout, tout.strip()[:90])
        check("task answered correctly", "FizzBuzz" in tout)
        check(
            "the task took several turns through the chain",
            len(rounds) > 1,
            f"{len(rounds)} model calls",
        )
        check(
            "tool results were carried back to the model",
            any(r["tool_result"] for r in rounds),
            f"max {max((r['tool_result'] for r in rounds), default=0)} per request",
        )
        check(
            "the agent actually wrote the file",
            os.path.exists(os.path.join(work, "fizz.py")),
        )
        check(
            "context grew across turns (the loop really accumulated)",
            len(rounds) > 1 and rounds[-1]["bytes"] > rounds[0]["bytes"],
            f"{rounds[0]['bytes']} -> {rounds[-1]['bytes']} bytes"
            if len(rounds) > 1
            else "",
        )
        paths = sorted({r["path"] for r in rounds})
        print(f"  distinct paths used by the task: {paths}")
        check(
            "no path outside AgentCore Gateway's inbound set",
            all(
                p.split("?")[0].removeprefix("/inference")
                in ("/v1/messages", "/v1/chat/completions", "/v1/responses", "/v1/models")
                for p in paths
            ),
            ",".join(paths),
        )

        if LITELLM_SMOKE_MODELS:
            step(
                "[multimodel] one /v1/messages 'hi' per non-chain model on the "
                "virtual key — sanity-checks LiteLLM's protocol handling for "
                "each upstream, no CLI, no tools"
            )
            # A fresh grant whose allowed_models covers every model the vk
            # permits, so the shim's fail-fast doesn't shadow LiteLLM's
            # protocol result. mint_agentcore accepts an explicit models list.
            sid_mm = f"ses-{secrets.token_hex(20)}"
            grant_mm = svc.mint_agentcore(
                sid_mm, "alice", spec, team="team-alpha",
                models=LITELLM_ALLOWED_MODELS,
            )
            token_mm = llm_shim.register(grant_mm) if grant_mm else ""
            # A 200 confirms end-to-end for that model; a non-200 is a data
            # point about how Anthropic Messages format survives LiteLLM's
            # translation to the upstream — for gpt-* / kimi-* against
            # /v1/messages this is not guaranteed to be lossless. Reported
            # inline; the [chain] step is what a green run is judged on.
            for m in LITELLM_SMOKE_MODELS:
                if not token_mm:
                    print(f"    {m}: could not mint a smoke grant")
                    continue
                code, body = shim_call(
                    token_mm,
                    {
                        "model": m,
                        "max_tokens": 8,
                        "messages": [{"role": "user", "content": "hi"}],
                    },
                )
                verdict = "ok" if code == 200 else "fail"
                print(f"    {verdict:>4}  {m:20} HTTP {code}  {body[:90]}")
            svc.revoke_agentcore(sid_mm)

        step("[failfast] shim refuses a model this session was not routed to")
        code, body = shim_call(local_token, {"model": "claude-opus-4-7", "max_tokens": 8,
                                            "messages": [{"role": "user", "content": "hi"}]})
        check("non-permitted model refused locally", code == 403, f"HTTP {code}")

        step("[scoping] the session policy narrows the credential to one gateway")
        code_ok, _ = raw_gateway_call(grant, fx.base_url(0), args.model)
        code_no, body_no = raw_gateway_call(grant, fx.base_url(1), args.model)
        check("credential works on its own gateway", code_ok == 200, f"HTTP {code_ok}")
        check(
            "same credential refused on a second gateway",
            code_no == 403,
            f"HTTP {code_no} {body_no[:80]}",
        )

        step("[revoke] revoking one session leaves another untouched")
        sid_b = f"ses-{secrets.token_hex(20)}"
        grant_b = svc.mint_agentcore(sid_b, "bob", spec, team="team-beta")
        check("second session minted", bool(grant_b))
        svc.revoke(sid_a)
        print("  revoke(sid_a) issued; polling for effect")
        t0 = time.time()
        revoked_at = None
        while time.time() - t0 < 120:
            code, _ = raw_gateway_call(grant, fx.base_url(0), args.model)
            if code != 200:
                revoked_at = time.time() - t0
                break
            time.sleep(3)
        check(
            "session A's credential stopped working",
            revoked_at is not None,
            f"after {revoked_at:.1f}s" if revoked_at else "still valid after 120s",
        )
        code_b, _ = raw_gateway_call(grant_b, fx.base_url(0), args.model)
        check("session B unaffected", code_b == 200, f"HTTP {code_b}")
        svc.revoke(sid_b)
    finally:
        for d in (
            os.path.join("/tmp", f"e2e-acgw-work-{SUFFIX}"),
            os.path.join("/tmp", f"e2e-acgw-cfg-{SUFFIX}"),
        ):
            shutil.rmtree(d, ignore_errors=True)
        if not args.keep:
            fx.destroy()
        else:
            print(f"\n--keep: left {fx.gateways}, roles {fx.gw_role}/{fx.caller_role}, "
                  f"table {fx.table}")

    print()
    if FAIL:
        print(f"FAILED ({len(FAIL)}): " + "; ".join(FAIL))
        return 1
    print("all assertions passed")
    return 0


def shim_call(local_token: str, body: dict) -> tuple[int, str]:
    """Call the loopback shim the way the CLI subprocess does."""
    req = urllib.request.Request(
        llm_shim_base() + "/v1/messages",
        data=json.dumps(body).encode(),
        headers={
            "content-type": "application/json",
            "authorization": f"Bearer {local_token}",
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, r.read().decode()[:200]
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:200]


def llm_shim_base() -> str:
    import llm_shim

    return llm_shim.BASE_URL


def raw_gateway_call(grant: dict, base_url: str, model: str) -> tuple[int, str]:
    """Sign and call the gateway directly, bypassing the shim.

    This is what a session's user could do with the credential they can read
    out of the kernel process, which is why the meaningful boundaries (session
    policy, revocation) are asserted through this path rather than the shim's.
    """
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest
    from botocore.credentials import Credentials

    data = json.dumps(
        {"model": model, "max_tokens": 8, "messages": [{"role": "user", "content": "hi"}]}
    )
    req = AWSRequest(
        method="POST",
        url=base_url.rstrip("/") + "/v1/messages",
        data=data,
        headers={"content-type": "application/json", "anthropic-version": "2023-06-01"},
    )
    SigV4Auth(
        Credentials(
            grant["access_key_id"], grant["secret_access_key"], grant.get("session_token")
        ),
        grant.get("service") or "bedrock-agentcore",
        grant["region"],
    ).add_auth(req)
    r = urllib.request.Request(
        req.url, data=data.encode(), headers=dict(req.headers), method="POST"
    )
    try:
        with urllib.request.urlopen(r, timeout=120) as resp:
            return resp.status, resp.read().decode()[:200]
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:200]


if __name__ == "__main__":
    sys.exit(main())
