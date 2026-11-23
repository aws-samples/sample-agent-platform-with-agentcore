# One MCP hub behind two AgentCore gateways: a signed-in user and an application with no user

A self-contained deployment of one pattern: the MCP hub runs as a pod, and
callers reach it only through Amazon Bedrock AgentCore Gateway. Two kinds of
caller, neither holding a static key:

- **A signed-in user**: Claude Code or Codex in a Coder workspace. The user signs in
  to Coder with the IdP and links it once more as Coder's external auth
  provider; Coder hands the workspace a fresh access token, which the gateway
  validates.
- **An application with no user**: a pod that signs its requests with its own
  IAM role (IRSA, SigV4).

```
caller cluster                                                   hub cluster (another VPC)
  Coder workspace          user JWT
    Claude Code, Codex -----------------> gateway <name>-jwt --+
    (Bedrock with the pod's role)         CUSTOM_JWT            |  VPC Lattice     internal NLB      hub pod
                                          Cedar: team claim     +- resource ----> TLS, private CA -> gateway_entry:
  static-app pod           SigV4                                |  gateway                           trust network
    IRSA role --------------------------> gateway <name>-iam --+
                                          AWS_IAM
  intruder pod (same IAM grant)           Cedar: caller role
```

A gateway takes one inbound authorizer type, so there are two gateways in
front of the same hub.

## What each layer decides

| Layer | JWT gateway | IAM gateway |
|---|---|---|
| Authentication | the IdP's signature, issuer and audience on the user's token | SigV4 and `bedrock-agentcore:InvokeGateway` on the caller's role |
| Authorization | Cedar, default-deny: `AgentCore::OAuthUser` whose `team` claim holds one of `allowed_teams` (one permit per team) | Cedar, default-deny: `AgentCore::IamEntity` equal to the static-app role's sessions |
| The hub's own rule (optional) | `hub_allowed_teams`: a user the gateway admitted runs a tool only in these teams | none: application-only calls are not judged by it |
| What reaches the hub | the user's token, exchanged by the gateway (RFC 8693) | nothing: no credential, no caller identity |
| What the hub knows | the user and their teams | only that the gateway admitted the call |

The last column is a limit of the service, not of this deployment: an MCP
server target cannot be given the caller's IAM identity
(`CALLER_IAM_CREDENTIALS` is not supported for that target type). Authorization
by application therefore lives entirely in the gateway's Cedar policy. If the
hub itself must tell applications apart, use the API Gateway entry in
[`../eks-iam-entry`](../eks-iam-entry), which forwards the caller's ARN.

## Two layers on users: the gateway's policy and the hub's own

`allowed_teams` is what the JWT gateway's Cedar policy admits;
`hub_allowed_teams` (optional) is a rule the hub enforces itself on top, as
`gateway_entry.allowed_departments` in its config. A user must pass both. The
deployed demo sets them apart on purpose:

| User | Team | Gateway (`allowed_teams`: team-a, team-c) | Hub (`hub_allowed_teams`: team-a) | `verify.sh` |
|---|---|---|---|---|
| alice | team-a | admitted | runs the tool | check 3 |
| bob | team-b | refused: no tool listed, the request never reaches the hub | - | check 4 |
| carol | team-c | admitted: the tool is listed | refused at call time (`hub policy: ...`), logged by the hub | check 4b |

The hub's rule applies at call time, not to `tools/list`: the gateway
synchronizes its tool list from the hub with its own service identity, not
per user, so hiding the tool there would hide it from everyone. Prefer the
gateway's policy (a refusal there never reaches the hub, and changing it
leaves the hub alone); the hub's rule is for a hub that must not rely on the
gateway's configuration alone. Changing `allowed_teams` replaces the team
policies one by one: the new ones are made ACTIVE before an old one is
deleted, so a team that stays allowed keeps its access throughout.

`intruder` holds the same IAM grant as `static-app` on purpose. IAM lets it
through the gateway's front door; the Cedar policy permits it nothing, so it
sees no tool and its call is refused. That is the difference between "may
talk to the gateway" and "may call this tool".

The role of the `coder` service account, which the Coder workspaces run
under, can invoke two Bedrock models and nothing else. It has no grant on
either gateway: tools are reachable only with the token of whoever signed in.

## Why the hub may trust a request with no credentials

The hub runs with `gateway_entry: {trust: network}` (see `hub/app.py`): a
`Bearer` is verified as a user token, and a request with no `Authorization` is
taken as an application the IAM gateway admitted. That is only sound if
nothing but the gateways can reach the listener. Three layers close the path:

| Path to the hub | Closed by | `verify.sh` |
|---|---|---|
| a workload in the hub VPC connects to the **NLB** | the NLB's security group admits the resource gateway's security group and nothing else | check 7, first probe |
| a pod connects to the **hub pod** directly | the pod's NetworkPolicy admits the NLB's subnets only; no pod runs in them | check 7, second probe |
| a workload connects to the **node's NodePort** | the node security group admits that port from the NLB's group only | check 7, third probe |

`k8s/render.py` refuses to write the manifests unless the rendered
NetworkPolicy admits exactly those subnets and the hub's embedded config is
exactly the gateway entry (an empty `from:` would admit every source). The
hub reads `AUDIENCE`, `ISSUER`, `RESOURCE_URL`, `JWKS_URL`, `HUB_CONFIG` and
`HUB_ACTORS` from its environment ahead of that config, so `render.py` also
pins the hub pod: one container, with exactly the environment, image,
command and mounts of the template, and no other workload in the manifest
carrying the hub's labels. To change the hub pod, change `render.py` with it.

TLS on that path uses a private CA created by the Terraform here: the CA
certificate is registered on the targets as their trust anchor, the server
certificate is imported into ACM for the NLB. Both private keys are in the
Terraform state. They are server-side TLS material, not caller credentials;
for anything longer-lived issue the certificate from AWS Private CA.

## Prerequisites

- The two clusters of [`../eks-iam-entry`](../eks-iam-entry) (this stack looks
  them up by name; on resources that stack owns it adds one rule to the hub
  cluster's node security group and attaches one target group to its node
  group, both as separate resources, and changes nothing else).
- An OIDC IdP reachable by the gateway, with:
  - an AgentCore Identity OAuth2 credential provider configured for token
    exchange at that IdP (`obo_credential_provider_name`);
  - for `verify.sh`, a public client allowing the password grant whose tokens
    carry the audience and the team claim (`verify_token_client_id`): a
    headless stand-in for signing in to Coder;
  - admin access for `coder/deploy.sh`, which creates Coder's own
    confidential client (`coder-demo`).
- Terraform >= 1.11 (write-only arguments), Helm 3, kubectl, the AWS CLI, Python 3 with PyYAML and boto3 >= 1.43.
- Amazon Bedrock access to the two `global.` Claude inference profiles in
  `claude_models` and the `global.openai.` profile in `codex_model`
  (default `global.openai.gpt-6.1-sol`).

This deployment was run against Keycloak only. With Okta as the IdP, from
Okta's documentation and not from a test here: use a custom authorization
server (the org server's tokens are not meant for third-party validation),
and note that Okta puts the client in `cid`, not `client_id`, so authorize by
audience and claims rather than `allowedClients`.

## Deploy

```bash
cd services/mcp-hub-demo/deploy/agentcore-gateway-entry/terraform
cp terraform.tfvars.example terraform.tfvars   # region, oidc_issuer_url
terraform init
terraform plan -out=gw.plan
terraform apply gw.plan                        # gateways, policy engines, NLB, IAM roles

cd ..
./deploy.sh                                    # pods, then gateway targets and Cedar policies
./verify.sh                                    # every path, from inside the pods
```

`deploy.sh` creates the targets after the hub pod is running, because a target
is synchronized against the hub when it is created. It is a script rather
than Terraform because the provider cannot set a target's private CA.

To remove: run `scripts/gateway_targets.py` with `--delete` on a file holding
`terraform output -json`, delete the two namespaces, then `terraform destroy`.

## Use it from Coder

`terraform/coder.tf` and `coder/` add a Coder server in front of the same
JWT gateway:

```
browser --HTTPS--> CloudFront (*.cloudfront.net, WAF: coder_allowed_cidrs, default block)
  --VPC origin--> internal ALB (caller lb subnets, SG: CloudFront's VPC origin group only)
  --> NodePort --> Coder server (namespace coder) --> RDS PostgreSQL
Coder workspace (pod in gw-demo, service account coder: Bedrock only)
  Claude Code --headersHelper: coder external-auth access-token keycloak--> JWT gateway
  Codex -------http_headers_helper: the same helper------------------------> JWT gateway
```

- **Who may open it** is decided once, by the WAF on the distribution. The
  ALB's security group cannot say it: behind CloudFront every request comes
  from CloudFront's addresses. The ALB has no public address.
- **Sign-in** is Keycloak only (`CODER_DISABLE_PASSWORD_AUTH`). The same
  confidential client, `coder-demo`, is Coder's OIDC client and its external
  auth provider `keycloak`; its tokens carry the gateway's audience, the team
  claim and `gateway-delegate`, as `portal-web`'s do, so the gateway and Cedar
  are unchanged.
- **The workspace token**: Coder keeps each user's refresh token in its
  database and refreshes the access token itself. The workspace's
  `headersHelper` asks for it on every connection; an `Authorization` header
  from a helper means Claude Code starts no OAuth sign-in of its own. No
  loopback callback, no port-forward. The Coder database therefore holds
  every linked user's Keycloak refresh token: treat it as sensitive.
- **Codex** uses the same helper for the same gateway. It runs the helper
  with a cleared environment (HOME, PATH, locale), so the agent's
  `CODER_AGENT_TOKEN` and `CODER_AGENT_URL` do not reach it: the startup
  script writes the agent URL into the helper and the agent token into a
  0600 file the helper reads. The token is already in every agent child's
  environment and in the pod spec; the file adds no reader. Codex's model
  is `codex_model`, called through its built-in `amazon-bedrock-runtime`
  provider (SigV4, the Responses API) with the pod's role, which is why that
  role also holds `project/default` (that API authorizes every request on
  the account's default project). Codex's own sandbox needs a user
  namespace the pod does not give it, so it runs with `danger-full-access`
  and `on-request` approvals: the pod is the boundary, as it is for Claude
  Code.
- **The agent connects in-cluster**: the template sets the Coder provider's
  `url` to the Service, so the workspace never goes through the WAF.

```bash
cd terraform && terraform plan -out=coder.plan && terraform apply coder.plan   # needs coder_allowed_cidrs
cd .. && ./coder/deploy.sh   # Keycloak client, Helm release, owner deploy token, template mcp-hub
```

Then open `terraform output -raw coder_url`, sign in with Keycloak, create a
workspace from `mcp-hub` (the create page asks to link Keycloak first) and
open its Claude Code or Codex app.

`coder/deploy.sh`, which can be run again at any time:

- With password sign-in disabled, Coder 2.38 refuses a password login to
  every user, the owner included (the documentation says owners are exempt;
  the code has no such exception). The first run therefore opens password
  sign-in, creates the owner, mints an API token for it and closes password
  sign-in again; the token (7 days, Coder's maximum here) is kept in a Secret
  and minted again when it has expired.
- No secret appears on a command line or in its output: each goes from one
  process to a Secret through a file in a temp directory, or into the pod on
  stdin.

Gotchas met on the way: a security group rule description may not contain an
apostrophe (the rule fails to create, the rest of the apply succeeds); and the
operator's egress changes with the network they are on, which the WAF
answers with CloudFront's generic 403 page.

## What `verify.sh` checks

1. `static-app` lists and calls the tool through the IAM gateway; the hub
   reports the gateway as the actor, with no caller ARN and no user.
2. `intruder` gets through IAM and is permitted nothing by Cedar.
3. A user both layers allow lists and calls the tool through the JWT
   gateway; the hub names the user (the token was exchanged, not dropped).
4. A user in another team has a valid token and is refused by Cedar.
   4b. (when `hub_allowed_teams` is set) A user the gateway admits but the
   hub does not: the tool is listed, the call is refused with the hub's own
   reason, and the hub's log records the refusal.
5. Each gateway refuses the other's kind of credential, and the JWT gateway's
   401 carries the discovery pointer an MCP client signs in from.
6. The coder role holds no `InvokeGateway` on either gateway.
7. A pod in the hub VPC reaches neither the NLB, nor the hub pod, nor its
   NodePort.
8. The Coder entry: this machine reaches Coder through CloudFront, the WAF
   blocks by default and allows one IP set, and the ALB resolves to private
   addresses only.

Checks 3 and 4 obtain tokens with the password grant as a headless stand-in
for the browser sign-in (`scripts/user_token.py`); nothing in the deployment
uses that grant.

## Offline tests

Five tests need no AWS and run in the merge-request pipeline:

- `../../scripts/test_gateway_entry_adversarial.py`: what the hub refuses
  behind a gateway, so that a malformed credential is never taken for an
  application-only call.
- `k8s/test_render_adversarial.py`: `render.py` writes nothing unless the
  hub's NetworkPolicy admits exactly the NLB's subnets, its config is
  exactly the gateway entry, and its pod can run on nothing but that config
  (no environment variable that overrides it, no other file, no second
  container or workload). It also runs the Coder workspace's startup script
  with stand-ins: the terminal finds claude, Codex is configured for the
  same gateway and helper, and the headers helper sends the user's token or
  nothing (never the sign-in URL), with the agent's environment or the
  cleared one Codex gives it.
- `client/test_gateway_mcp_client_adversarial.py`: the client `verify.sh`
  trusts reports "denied" only for the answer a policy refusal is, never for
  a request that merely failed or an answer to another request; it follows no
  redirect, so its credential reaches the gateway URL and nothing else.
- `test_verify_adversarial.py`: `verify.sh` itself, run with stand-ins for
  terraform, kubectl, aws and curl, passes a deployment that behaves and
  fails exactly the one check whose evidence is a near miss: a DNS, TLS or
  refused-connection error where a dropped connection must time out, a 403
  that is not the resource policy's refusal, a call that succeeded where it
  must be refused, a forged RESULT record. The transport errors and 403s
  come from the real client against a local fake gateway.
- `scripts/test_policies_adversarial.py`: the two Cedar templates, rendered
  and evaluated over claim values as the service delivers them, permit
  exactly who they should; rendering refuses a value that would widen a
  policy; `user_token.py` sends a password only to a plain https issuer.

## What the team policy matches, and its limit

A JWT claim reaches Cedar as a string, a list as its JSON text
(`["team-a","team-b"]`). The policy therefore spells membership out as the
positions an entry can take in that text (the only entry, the first, the
last, one in between, or a bare string), each pattern anchored on the list
brackets. It does not match "the quoted name appears somewhere", which a
single string such as `other "team-a" group` would satisfy.

This is pattern matching on text, not parsing: Cedar here has string
equality and `like`, and no way to read JSON. It is sound for one shape of
claim only, a flat list of strings, and wrong outside it. Before reusing it:

- **A claim that is not a flat list of strings can be permitted wrongly.**
  The "one in between" pattern asks for `,"team-a",` anywhere inside the
  outer brackets, so it also matches a nested list
  (`[["x","team-a","y"]]`) and a list of objects with the name between them
  (`[{"a":"x"},"team-a",{"b":"y"}]`). A single string claim whose text is
  itself `["x","team-a","y"]` is indistinguishable from the list. None of
  these is what Keycloak's group mapper issues, but the policy does not
  refuse them.
- **For anything beyond a demo, authorize on a single-valued claim.** Have
  the IdP issue one string per decision (for example a claim that is
  `"true"` only for members of the allowed team, or the user's one primary
  team) and compare it with `==`. That needs no pattern and has none of
  these limits.
- The list text carries no space after a comma. That is what reached Cedar
  from Keycloak's tokens in this deployment; it has not been checked with
  another IdP. In a list written with spaces the allowed team still matches
  as the only or the first entry but not in a later position, so such a
  member is denied (never an outsider admitted): the failure is closed, but
  look at a real token first.
- A team whose own name embeds the pattern (a group literally named
  `x","team-a`) could still match. Team names come from the IdP's group
  names; keep quotes and commas out of them.
