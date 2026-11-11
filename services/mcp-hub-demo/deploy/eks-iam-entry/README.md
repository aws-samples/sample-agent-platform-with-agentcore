# Pod to pod over IAM: an EKS application calling the MCP hub through API Gateway

A minimal, self-contained deployment of one pattern: an application running
as a pod on Amazon EKS calls the MCP hub, also a pod, through a private
Amazon API Gateway that authenticates the application with AWS IAM. The two
pods sit in **different VPCs**, and **no static key exists anywhere** on the
path - no access key, no shared header secret, no API key.

```
VPC-A (10.61.0.0/16)                         VPC-B (10.62.0.0/16)
cluster A                                    cluster B
  frontend pod                                 hub pod (services/mcp-hub-demo/hub)
  IRSA role <name>-frontend                    entry_identity: trust network
    |                                              ^
    | SigV4 (execute-api), credentials from        | :8000, only from the lb subnets
    | the pod's projected service-account token    | (NetworkPolicy)
    v                                              |
  execute-api endpoint ---> private REST API ---VPC Link---> internal NLB -> node :30080
  (lb subnets, private DNS)  AWS_IAM on /mcp                 (lb subnets)
                             resource policy: this endpoint
                             AND the frontend role
                             sets x-caller-arn
```

The VPCs are not peered or routed to each other. The endpoint in VPC-A and
the VPC Link into VPC-B are the only way across, and the API is the only
thing that can use both.

## What each hop proves

| Hop | Mechanism | What it establishes |
|---|---|---|
| pod -> AWS | IRSA: EKS projects a service-account token, botocore exchanges it with `sts:AssumeRoleWithWebIdentity` and refreshes it | the pod holds short-lived credentials of exactly one role, which trusts exactly one service account |
| pod -> API | SigV4 for `execute-api`, method authorization `AWS_IAM` | the request was signed by those credentials (body included) |
| API resource policy | explicit **Deny** unless `aws:SourceVpce` = VPC-A's endpoint, explicit **Deny** unless `aws:PrincipalArn` = the frontend role (plus the matching Allow) | only that role, only from that endpoint, may invoke at all - whatever any identity policy in the account allows |
| API -> hub | integration header `x-caller-arn` = `context.identity.userArn` | the hub learns which role called; a caller-supplied `x-caller-arn` is overwritten |
| hub | `entry_identity` with `trust: network` and `callers` | the hub maps the role to an actor; it trusts the header because nothing but the API's VPC Link can reach it (below) |

The resource policy's restrictions are Denies, not conditions on an Allow,
on purpose. With `AWS_IAM` and caller and API in the same account, API
Gateway admits a request that either the caller's identity policy or the
resource policy allows. A conditional Allow would only add a second way in:
the frontend role's own `execute-api:Invoke` grant would still admit it
through any other execute-api endpoint it could reach. An explicit Deny wins
over every Allow, which is the pattern AWS documents for private APIs.

The last row is the one design decision to understand. The platform's own
IAM entry (`terraform/modules/mcp_hub_demo/entry.tf`) adds a shared secret
header that the hub checks, so a VPC neighbour that reaches the NLB directly
cannot forge `x-caller-arn`. That secret is a static key, which this
deployment must not have, so here the guarantee comes from the network
instead. Three layers, each closing a different path to the hub:

| Path to the hub | Closed by | `verify.sh` |
|---|---|---|
| a VPC-B workload connects to the **NLB** | the NLB's security group has **no inbound rule**. The VPC Link arrives over PrivateLink, which the group does not evaluate (`enforce_security_group_inbound_rules_on_private_link_traffic = off`), so the API still gets in; nothing else does | check 6 |
| a VPC-B workload connects to the **node's NodePort** | the node security group admits the NodePort from the NLB's group only | - |
| a pod connects to the **hub pod** directly | the hub pod's NetworkPolicy (VPC CNI network policy agent) admits port 8000 from the NLB's dedicated subnets only; no pod runs in them. The Service uses `externalTrafficPolicy: Local`, so the packet keeps the NLB's address | check 5 |

The first layer is the one that is easy to miss. The NetworkPolicy cannot
tell the VPC Link's traffic from anyone else's once both have passed through
the NLB: they arrive from the same lb-subnet addresses. If the NLB admitted
traffic from inside VPC-B, any pod there could send a forged `x-caller-arn`
through it and the hub would believe it (an earlier revision of this stack
did exactly that, reproduced against a live deployment). Keep the NLB's
inbound rules empty, and do not put anything else behind it.

The third layer can also fail open by accident: a NetworkPolicy ingress rule
whose `from:` list is empty admits every source. `deploy.sh` therefore
renders through `k8s/render.py`, which refuses (and writes nothing) when the
NLB subnet list is empty or not narrow IPv4 CIDRs, when any template
variable is empty, and unless the rendered policy admits exactly those
subnets; `k8s/test_render_adversarial.py` attacks it offline.

If you cannot guarantee this isolation for your hub, keep `trust: secret`.
That is still keyless for the calling application, though not for the
gateway.

`callers` maps a **role** to an actor, not a session name: under IRSA the SDK
names the session itself (`botocore-session-<timestamp>`), so the role is the
stable identity. Partition, account and role name must all match.

## What the hub does with an application-only call

`require_user_token: false` lets a call through the entry carry no end-user
token. Such a call has no department claim, so the department-gated
backends stay out of reach. The hub exposes one built-in tool,
`hub__whoami`, which returns the actor and the ARN the gateway authenticated.
That is what the test asserts on. To route application-only calls to real
backends, give those backends a way to authorize an application (for
example a routing entry keyed on the actor); the demo backends verify a user
JWT and are not wired here.

## Deploy

Prerequisites: Terraform >= 1.9, kubectl, the AWS CLI with credentials for
the target account, and Python 3 with PyYAML on the operator machine
(`k8s/render.py` parses the rendered NetworkPolicy back before anything is
applied). The pods use the
public `python:3.12-slim` image from Amazon ECR Public and pip-install their
dependencies at start, so the nodes need outbound internet through the NAT
gateway; nothing is built or pushed.

```bash
cd services/mcp-hub-demo/deploy/eks-iam-entry/terraform
cp terraform.tfvars.example terraform.tfvars   # set eks_public_access_cidrs to your egress /32
terraform init
terraform plan -out=tf.plan
terraform apply tf.plan                        # ~15-20 minutes: two EKS clusters

cd ..
./deploy.sh                                    # hub into cluster B, frontend + intruder into cluster A
./verify.sh                                    # all checks from inside the pods
```

The stack creates IAM roles (cluster, node, and the two IRSA roles), so the
applying principal needs IAM permissions. The API stage's access logs need
the account-level API Gateway CloudWatch role (`aws apigateway
get-account`); set it once per account and Region if it is missing.

## What `verify.sh` checks

1. The frontend pod completes an MCP exchange (`initialize`, `tools/list`,
   `tools/call hub__whoami`) through the API, and the hub names a session of
   the frontend IRSA role as the caller, with no user attached.
2. The intruder pod - same namespace, same node, same network path, a
   different IRSA role - is refused by API Gateway (403). It never reaches
   the hub.
3. An unsigned request is refused by API Gateway (403), even when it carries
   a hand-written `x-caller-arn`.
4. A signed request that carries a forged `x-caller-arn` reaches the hub
   with the gateway's value, not the forged one.
5. A pod beside the hub in cluster B cannot connect to the hub pod at all.
6. The same pod cannot reach the hub through its NLB either: any HTTP answer,
   even a refusal from the hub, fails the check, because it would mean a
   forged header reached the hub from a trusted address.
7. Neither pod has `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` or
   `AWS_SESSION_TOKEN` in its environment (each pod read separately; a pod
   that cannot be read fails the check). The frontend's credentials are
   resolved by `AssumeRoleWithWebIdentity`.

The client reports success only when every step of the MCP exchange
succeeded: an HTTP or JSON-RPC error at any step ends it with `ok: false` and
the failing step.

## Using the pattern in an application

`client/sigv4_mcp_client.py` speaks MCP over HTTP by hand so the signing is
visible, with boto3 and the standard library only.
`client/test_sigv4_mcp_client_adversarial.py` runs it offline against a fake
hub that fails each step every way it can, and requires that none of them
ends in `ok: true` - `verify.sh` trusts the client's verdict. In an application, put the same three lines (default credential
chain, `AWSRequest`, `SigV4Auth(..., "execute-api", region)`) into an
`httpx.Auth` and hand that client to the MCP SDK's `streamable_http_client`.
Re-read the credentials per request (`session.get_credentials()` refreshes
the IRSA session before it expires).

## Cost and cleanup

Two EKS control planes, two `t4g.medium` nodes, two NAT gateways, one NLB and
one interface endpoint (two AZs). Delete everything when done:

```bash
cd terraform && terraform destroy
```
