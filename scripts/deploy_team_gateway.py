#!/usr/bin/env python3
"""Create the AgentCore Gateway for the team-auth demo (OBO token exchange).

Gateway shape (CloudFormation does not cover these target/credential types
yet, hence this script):

- **Inbound**: CUSTOM_JWT against the Keycloak realm (TeamAuthStack) —
  callers present the end user's access token; the gateway validates
  signature, issuer and the ``agent-platform`` audience.
- **Targets**: two MCP-server targets (the team API containers behind
  CloudFront), with their tool schemas declared inline so target creation
  never needs to call the (token-protected) servers.
- **Outbound (team-a / team-b)**: **OAuth token exchange (RFC 8693,
  on-behalf-of)** — for every tool call, AgentCore Identity exchanges the
  inbound *user* token at Keycloak (as the ``gateway-delegate`` confidential
  client) for a fresh token that still carries the user's ``sub`` and
  ``team`` claims. The team APIs validate that token against the IdP JWKS
  and enforce the team claim themselves — app-layer authorization; no authz
  logic in the gateway.

  (Plain ``JWT_PASSTHROUGH`` is not supported for MCP-server targets — the
  API rejects it; token exchange is the documented pattern for this shape
  and is what production should use anyway: the downstream token is
  audience-scoped instead of a replay of the login token.)

- **team-c (the not-yet-SSO-adapted backend)**: the third target models a
  freshly built internal API that cannot validate IdP tokens yet. Two
  AgentCore capabilities stand in for the missing SSO support:

  * a **Lambda REQUEST interceptor** on the gateway inspects every
    ``tools/call`` for ``team-c___*`` tools and rejects callers whose JWT
    (already signature/issuer/audience-verified by the gateway's inbound
    authorizer) lacks the ``team-c`` claim — authorization happens in
    AgentCore, before the request ever reaches the backend;
  * an **API-key credential provider** injects the backend's static
    ``X-Api-Key`` outbound, so the auth-less service still is not open to
    direct callers.

Prerequisites: TeamAuthStack deployed and scripts/seed_team_idp.py run (it
pins the delegate client secret in Secrets Manager and inside Keycloak).

Idempotent: re-running reuses the gateway/provider/targets by name. The
resulting wiring is stored in SSM parameter ``/agent-platform/team-gateway``
for the backend and the E2E suite.

Usage:
    python3 scripts/deploy_team_gateway.py            # create/update
    python3 scripts/deploy_team_gateway.py --delete   # tear down
"""

import argparse
import io
import json
import sys
import time
import zipfile

import boto3

GATEWAY_NAME = "agent-platform-team"
ROLE_NAME = "agent-platform-team-gateway-role"
PROVIDER_NAME = "agent-platform-keycloak-delegate"
OBO_TEAMS = ["team-a", "team-b"]  # SSO-capable backends (OAuth token exchange)
TEAM_C = "team-c"  # no-SSO backend (API key out, Lambda interceptor authz)
SSM_PARAM = "/agent-platform/team-gateway"
DELEGATE_SECRET = "agent-platform/gateway-delegate"  # nosec B105 - secret name
TEAM_C_KEY_SECRET = "agent-platform/team-c-api-key"  # nosec B105 - secret name
API_KEY_PROVIDER_NAME = "agent-platform-team-c-key"
INTERCEPTOR_FN = "agent-platform-team-gw-interceptor"
INTERCEPTOR_ROLE = "agent-platform-team-gw-interceptor-role"
AUDIENCE = "agent-platform"

# The gateway REQUEST interceptor: AgentCore-side team authorization for the
# no-SSO team-c backend. The gateway's CUSTOM_JWT inbound authorizer has
# already verified the token's signature, issuer and audience by the time
# this runs — the function only reads the (trusted) claims.
INTERCEPTOR_CODE = '''\
import base64
import json

TEAM = "team-c"
PREFIX = "team-c___"  # gateway tool names are "<target>___<tool>"


def _claims(token: str) -> dict:
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


def handler(event, context):
    request = (event.get("mcp") or {}).get("gatewayRequest") or {}
    body = request.get("body") or {}
    passthrough = {
        "interceptorOutputVersion": "1.0",
        "mcp": {"transformedGatewayRequest": {"body": body}},
    }
    # Only tools/call on the no-SSO target is gated here; everything else
    # (initialize, tools/list, team-a/b calls) passes through untouched —
    # team-a/b enforce authorization themselves at the application layer.
    if body.get("method") != "tools/call":
        return passthrough
    tool = (body.get("params") or {}).get("name") or ""
    if not tool.startswith(PREFIX):
        return passthrough

    auth = next(
        (v for k, v in (request.get("headers") or {}).items()
         if k.lower() == "authorization"),
        "",
    )
    teams, user = [], None
    if auth.lower().startswith("bearer "):
        try:
            claims = _claims(auth[7:])
            raw = claims.get("team") or []
            teams = [raw] if isinstance(raw, str) else list(raw)
            user = claims.get("preferred_username") or claims.get("sub")
        except Exception:  # noqa: BLE001 - malformed token -> deny below
            pass
    teams = [t.strip("/") for t in teams]

    if TEAM in teams:
        print(f"ALLOW {tool}: user={user} teams={teams}")
        return passthrough

    print(f"DENY {tool}: user={user} teams={teams} (requires {TEAM})")
    return {
        "interceptorOutputVersion": "1.0",
        "mcp": {
            "transformedGatewayResponse": {
                "statusCode": 403,
                "body": {
                    "jsonrpc": "2.0",
                    "id": body.get("id"),
                    "error": {
                        "code": -32003,
                        "message": (
                            "access denied by gateway interceptor: caller "
                            f"belongs to {teams or 'no team'}, tools of "
                            f"{TEAM} require {TEAM} membership (this backend "
                            "has no SSO capability, so AgentCore enforces "
                            "team authorization before forwarding)"
                        ),
                    },
                },
            }
        },
    }
'''


def _team_auth_outputs(cfn) -> dict:
    outputs = cfn.describe_stacks(StackName="AgentPlatformTeamAuth")["Stacks"][0]["Outputs"]
    return {o["OutputKey"]: o["OutputValue"] for o in outputs}


def _ensure_role(iam, account: str) -> str:
    """Gateway service role (CreateGateway requires one; token exchange runs
    through AgentCore Identity, so the role itself needs no target perms)."""
    trust = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
                "Action": "sts:AssumeRole",
                "Condition": {"StringEquals": {"aws:SourceAccount": account}},
            }
        ],
    }
    try:
        role = iam.get_role(RoleName=ROLE_NAME)["Role"]
    except iam.exceptions.NoSuchEntityException:
        role = iam.create_role(
            RoleName=ROLE_NAME,
            AssumeRolePolicyDocument=json.dumps(trust),
            Description="Service role for the agent-platform team-auth demo gateway",
        )["Role"]
        print(f"created role {ROLE_NAME}")
        time.sleep(10)  # IAM propagation
    return role["Arn"]


def _ensure_credential_provider(control, discovery: str, sm) -> tuple[str, str]:
    """OAuth2 credential provider = the Keycloak gateway-delegate client.
    Returns (provider ARN, managed client-secret ARN)."""
    names = {
        p["name"]
        for p in control.list_oauth2_credential_providers(maxResults=20).get(
            "credentialProviders", []
        )
    }
    if PROVIDER_NAME not in names:
        delegate = json.loads(sm.get_secret_value(SecretId=DELEGATE_SECRET)["SecretString"])
        control.create_oauth2_credential_provider(
            name=PROVIDER_NAME,
            credentialProviderVendor="CustomOauth2",
            oauth2ProviderConfigInput={
                "customOauth2ProviderConfig": {
                    "oauthDiscovery": {"discoveryUrl": discovery},
                    "clientId": delegate["client_id"],
                    "clientSecret": delegate["client_secret"],
                    # RFC 8693: inbound user token becomes the subject_token;
                    # the delegate authenticates with its client secret (no
                    # separate actor token).
                    "onBehalfOfTokenExchangeConfig": {
                        "grantType": "TOKEN_EXCHANGE",
                        "tokenExchangeGrantTypeConfig": {"actorTokenContent": "NONE"},
                    },
                }
            },
        )
    detail = control.get_oauth2_credential_provider(name=PROVIDER_NAME)
    arn = detail["credentialProviderArn"]
    secret_arn = detail["clientSecretArn"]["secretArn"]
    print(f"credential provider: {arn}")
    return arn, secret_arn


def _ensure_api_key_provider(control, sm) -> tuple[str, str]:
    """API-key credential provider = the team-c backend's static key (held in
    Secrets Manager by TeamAuthStack). Returns (provider ARN, managed secret
    ARN)."""
    names = {
        p["name"]
        for p in control.list_api_key_credential_providers(maxResults=20).get(
            "credentialProviders", []
        )
    }
    if API_KEY_PROVIDER_NAME not in names:
        key = json.loads(sm.get_secret_value(SecretId=TEAM_C_KEY_SECRET)["SecretString"])
        control.create_api_key_credential_provider(
            name=API_KEY_PROVIDER_NAME,
            apiKey=key["api_key"],
        )
    detail = control.get_api_key_credential_provider(name=API_KEY_PROVIDER_NAME)
    arn = detail["credentialProviderArn"]
    secret_arn = detail["apiKeySecretArn"]["secretArn"]
    print(f"api-key credential provider: {arn}")
    return arn, secret_arn


def _ensure_interceptor_lambda(lam, iam, account: str) -> str:
    """The gateway REQUEST interceptor function (inline zip, stdlib only)."""
    trust = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "lambda.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }
        ],
    }
    try:
        role = iam.get_role(RoleName=INTERCEPTOR_ROLE)["Role"]
    except iam.exceptions.NoSuchEntityException:
        role = iam.create_role(
            RoleName=INTERCEPTOR_ROLE,
            AssumeRolePolicyDocument=json.dumps(trust),
            Description="Execution role for the team-gateway REQUEST interceptor",
        )["Role"]
        iam.attach_role_policy(
            RoleName=INTERCEPTOR_ROLE,
            PolicyArn="arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
        )
        print(f"created role {INTERCEPTOR_ROLE}")
        time.sleep(10)  # IAM propagation

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("index.py", INTERCEPTOR_CODE)
    code = buf.getvalue()

    try:
        fn = lam.get_function(FunctionName=INTERCEPTOR_FN)["Configuration"]
        lam.update_function_code(FunctionName=INTERCEPTOR_FN, ZipFile=code)
        print(f"interceptor lambda updated: {fn['FunctionArn']}")
        return fn["FunctionArn"]
    except lam.exceptions.ResourceNotFoundException:
        pass
    for attempt in range(6):
        try:
            fn = lam.create_function(
                FunctionName=INTERCEPTOR_FN,
                Runtime="python3.13",
                Role=role["Arn"],
                Handler="index.handler",
                Code={"ZipFile": code},
                Timeout=10,
                MemorySize=128,
                Description="Team authz for the no-SSO team-c gateway target",
            )
            print(f"interceptor lambda created: {fn['FunctionArn']}")
            return fn["FunctionArn"]
        except lam.exceptions.InvalidParameterValueException as exc:
            # freshly created role may not be assumable yet
            if "assume" not in str(exc).lower() or attempt == 5:
                raise
            time.sleep(10)
    raise RuntimeError("unreachable")


def _attach_gateway_policy(
    iam,
    account: str,
    region: str,
    provider_arn: str,
    secret_arn: str,
    api_key_provider_arn: str,
    api_key_secret_arn: str,
    interceptor_arn: str,
):
    """OAuth/API-key outbound needs the gateway service role to fetch
    credentials through AgentCore Identity (docs: gateway-outbound-auth,
    custom service role); the interceptor needs lambda:InvokeFunction."""
    identity_resources = [
        f"arn:aws:bedrock-agentcore:{region}:{account}:token-vault/default",
        f"arn:aws:bedrock-agentcore:{region}:{account}:workload-identity-directory/default",
        f"arn:aws:bedrock-agentcore:{region}:{account}:workload-identity-directory/default/workload-identity/{GATEWAY_NAME}-*",
    ]
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "GetWorkloadAccessToken",
                "Effect": "Allow",
                # a JWT-inbound gateway binds the caller's JWT to its
                # workload identity via the ForJWT variant — the docs' sample
                # policy lists only the base action, which is not enough
                "Action": [
                    "bedrock-agentcore:GetWorkloadAccessToken",
                    "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
                    "bedrock-agentcore:GetWorkloadAccessTokenForUserId",
                ],
                "Resource": [
                    f"arn:aws:bedrock-agentcore:{region}:{account}:workload-identity-directory/default",
                    f"arn:aws:bedrock-agentcore:{region}:{account}:workload-identity-directory/default/workload-identity/{GATEWAY_NAME}-*",
                ],
            },
            {
                "Sid": "GetResourceOauth2Token",
                "Effect": "Allow",
                "Action": ["bedrock-agentcore:GetResourceOauth2Token"],
                # IAM evaluates this action against the WORKLOAD IDENTITY
                # (CloudTrail-verified), not just the credential provider the
                # docs sample lists — include both resource families
                "Resource": [provider_arn, *identity_resources],
            },
            {
                "Sid": "GetResourceApiKey",
                "Effect": "Allow",
                "Action": ["bedrock-agentcore:GetResourceApiKey"],
                # same IAM-evaluation caveat as the OAuth action above
                "Resource": [api_key_provider_arn, *identity_resources],
            },
            {
                "Sid": "GetSecretValue",
                "Effect": "Allow",
                "Action": ["secretsmanager:GetSecretValue"],
                "Resource": [secret_arn, api_key_secret_arn],
            },
            {
                "Sid": "InvokeInterceptor",
                "Effect": "Allow",
                "Action": ["lambda:InvokeFunction"],
                "Resource": [interceptor_arn],
            },
        ],
    }
    iam.put_role_policy(
        RoleName=ROLE_NAME,
        PolicyName="team-gateway-obo",
        PolicyDocument=json.dumps(policy),
    )
    print("gateway role policy attached (OBO + API key + interceptor invoke)")


def _wait(desc, fetch, ready_status="READY", timeout=300):
    for _ in range(timeout // 5):
        status = fetch()
        if status == ready_status:
            print(f"{desc}: {status}")
            return
        if status.startswith(("FAILED", "DELETE")):
            raise RuntimeError(f"{desc} entered {status}")
        time.sleep(5)
    raise TimeoutError(f"{desc} not {ready_status} after {timeout}s")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", help="CloudFront base URL of TeamAuthStack")
    parser.add_argument("--region", default=None)
    parser.add_argument("--delete", action="store_true")
    args = parser.parse_args()

    session = boto3.Session(region_name=args.region)
    region = session.region_name
    account = session.client("sts").get_caller_identity()["Account"]
    control = session.client("bedrock-agentcore-control")
    cfn = session.client("cloudformation")
    iam = session.client("iam")
    ssm = session.client("ssm")
    sm = session.client("secretsmanager")

    existing = {g["name"]: g for g in control.list_gateways().get("items", [])}

    if args.delete:
        lam = session.client("lambda")
        gw = existing.get(GATEWAY_NAME)
        if gw:
            gid = gw["gatewayId"]
            for t in control.list_gateway_targets(gatewayIdentifier=gid).get("items", []):
                control.delete_gateway_target(gatewayIdentifier=gid, targetId=t["targetId"])
                print(f"deleted target {t['name']}")
            time.sleep(10)
            control.delete_gateway(gatewayIdentifier=gid)
            print(f"deleted gateway {gid}")
        for p in control.list_oauth2_credential_providers(maxResults=20).get(
            "credentialProviders", []
        ):
            if p["name"] == PROVIDER_NAME:
                control.delete_oauth2_credential_provider(name=PROVIDER_NAME)
                print(f"deleted credential provider {PROVIDER_NAME}")
        for p in control.list_api_key_credential_providers(maxResults=20).get(
            "credentialProviders", []
        ):
            if p["name"] == API_KEY_PROVIDER_NAME:
                control.delete_api_key_credential_provider(name=API_KEY_PROVIDER_NAME)
                print(f"deleted credential provider {API_KEY_PROVIDER_NAME}")
        try:
            lam.delete_function(FunctionName=INTERCEPTOR_FN)
            print(f"deleted lambda {INTERCEPTOR_FN}")
        except lam.exceptions.ResourceNotFoundException:
            pass
        try:
            iam.detach_role_policy(
                RoleName=INTERCEPTOR_ROLE,
                PolicyArn="arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
            )
            iam.delete_role(RoleName=INTERCEPTOR_ROLE)
            print(f"deleted role {INTERCEPTOR_ROLE}")
        except iam.exceptions.NoSuchEntityException:
            pass
        try:
            ssm.delete_parameter(Name=SSM_PARAM)
        except ssm.exceptions.ParameterNotFound:
            pass
        return 0

    base_url = args.base_url or _team_auth_outputs(cfn)["TeamAuthUrl"]
    base_url = base_url.rstrip("/")
    issuer = f"{base_url}/realms/agent-platform"
    discovery = f"{issuer}/.well-known/openid-configuration"
    print(f"IdP discovery: {discovery}")

    lam = session.client("lambda")
    role_arn = _ensure_role(iam, account)
    provider_arn, secret_arn = _ensure_credential_provider(control, discovery, sm)
    api_key_provider_arn, api_key_secret_arn = _ensure_api_key_provider(control, sm)
    interceptor_arn = _ensure_interceptor_lambda(lam, iam, account)
    _attach_gateway_policy(
        iam,
        account,
        region,
        provider_arn,
        secret_arn,
        api_key_provider_arn,
        api_key_secret_arn,
        interceptor_arn,
    )

    # ------------------------------ gateway -----------------------------
    # The REQUEST interceptor needs the raw Authorization header to read the
    # (gateway-verified) team claim, hence passRequestHeaders.
    interceptor_configs = [
        {
            "interceptor": {"lambda": {"arn": interceptor_arn}},
            "interceptionPoints": ["REQUEST"],
            "inputConfiguration": {"passRequestHeaders": True},
        }
    ]
    if GATEWAY_NAME in existing:
        gateway_id = existing[GATEWAY_NAME]["gatewayId"]
        print(f"gateway exists: {gateway_id}")
        detail = control.get_gateway(gatewayIdentifier=gateway_id)
        current = detail.get("interceptorConfigurations") or []
        if current != interceptor_configs:
            update_kwargs = {}
            if detail.get("description"):
                update_kwargs["description"] = detail["description"]
            control.update_gateway(
                gatewayIdentifier=gateway_id,
                name=detail["name"],
                roleArn=detail["roleArn"],
                protocolType=detail["protocolType"],
                authorizerType=detail["authorizerType"],
                authorizerConfiguration=detail["authorizerConfiguration"],
                exceptionLevel=detail.get("exceptionLevel") or "DEBUG",
                interceptorConfigurations=interceptor_configs,
                **update_kwargs,
            )
            print("gateway updated: REQUEST interceptor attached")
    else:
        resp = control.create_gateway(
            name=GATEWAY_NAME,
            description="Team-auth demo: user JWT in, OBO token exchange out to team APIs",
            roleArn=role_arn,
            protocolType="MCP",
            authorizerType="CUSTOM_JWT",
            authorizerConfiguration={
                "customJWTAuthorizer": {
                    "discoveryUrl": discovery,
                    "allowedAudience": [AUDIENCE],
                }
            },
            exceptionLevel="DEBUG",
            interceptorConfigurations=interceptor_configs,
        )
        gateway_id = resp["gatewayId"]
        print(f"created gateway: {gateway_id}")

    _wait("gateway", lambda: control.get_gateway(gatewayIdentifier=gateway_id)["status"])
    gw_detail = control.get_gateway(gatewayIdentifier=gateway_id)
    mcp_url = gw_detail.get("gatewayUrl") or (
        f"https://{gateway_id}.gateway.bedrock-agentcore.{region}.amazonaws.com/mcp"
    )

    # ------------------------------ targets -----------------------------
    current_targets = {
        t["name"]: t
        for t in control.list_gateway_targets(gatewayIdentifier=gateway_id).get("items", [])
    }
    for team in OBO_TEAMS:
        if team in current_targets:
            print(f"target exists: {team}")
            continue
        control.create_gateway_target(
            gatewayIdentifier=gateway_id,
            name=team,
            description=f"{team} backend API (app-layer SSO authz, OBO outbound)",
            targetConfiguration={
                "mcp": {
                    "mcpServer": {
                        "endpoint": f"{base_url}/{team}/mcp",
                        # DYNAMIC: the gateway lists tools live, per caller,
                        # using the caller's exchanged token (an upfront
                        # mcpToolSchema is only allowed for the
                        # AUTHORIZATION_CODE grant)
                        "listingMode": "DYNAMIC",
                    }
                }
            },
            credentialProviderConfigurations=[
                {
                    "credentialProviderType": "OAUTH",
                    "credentialProvider": {
                        "oauthCredentialProvider": {
                            "providerArn": provider_arn,
                            "grantType": "TOKEN_EXCHANGE",
                            "scopes": [],
                            "customParameters": {
                                "subject_token_type": (
                                    "urn:ietf:params:oauth:token-type:access_token"
                                )
                            },
                        }
                    },
                }
            ],
        )
        print(f"created target: {team}")

    if TEAM_C in current_targets:
        print(f"target exists: {TEAM_C}")
    else:
        control.create_gateway_target(
            gatewayIdentifier=gateway_id,
            name=TEAM_C,
            description=(
                f"{TEAM_C} backend API (no SSO capability — team authz enforced "
                "by the gateway's Lambda REQUEST interceptor; static API key out)"
            ),
            targetConfiguration={
                "mcp": {
                    "mcpServer": {
                        "endpoint": f"{base_url}/{TEAM_C}/mcp",
                        "listingMode": "DYNAMIC",
                    }
                }
            },
            credentialProviderConfigurations=[
                {
                    "credentialProviderType": "API_KEY",
                    "credentialProvider": {
                        "apiKeyCredentialProvider": {
                            "providerArn": api_key_provider_arn,
                            "credentialParameterName": "X-Api-Key",
                            "credentialLocation": "HEADER",
                        }
                    },
                }
            ],
        )
        print(f"created target: {TEAM_C}")

    all_teams = [*OBO_TEAMS, TEAM_C]
    for team in all_teams:
        _wait(
            f"target {team}",
            lambda team=team: next(
                t["status"]
                for t in control.list_gateway_targets(gatewayIdentifier=gateway_id)["items"]
                if t["name"] == team
            ),
        )

    payload = {
        "gateway_id": gateway_id,
        "mcp_url": mcp_url,
        "issuer": issuer,
        "teams": all_teams,
        "team_api_base": base_url,
        "interceptor_lambda": interceptor_arn,
    }
    # include the JWT-inbound demo runtime if TeamDemoStack is deployed
    # (re-run this script after `cdk deploy AgentPlatformTeamDemo` to pick it up)
    try:
        outputs = cfn.describe_stacks(StackName="AgentPlatformTeamDemo")["Stacks"][0]["Outputs"]
        payload["runtime_arn"] = next(
            o["OutputValue"] for o in outputs if o["OutputKey"] == "TeamDemoRuntimeArn"
        )
    except Exception:  # noqa: BLE001
        print("note: AgentPlatformTeamDemo not deployed yet — runtime_arn omitted")

    ssm.put_parameter(Name=SSM_PARAM, Type="String", Value=json.dumps(payload), Overwrite=True)
    print(f"\nstored in SSM {SSM_PARAM}:")
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
