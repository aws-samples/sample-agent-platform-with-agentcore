"""SOP generation for IAM service channels.

The deliberate division of labor: IAM answers *who is calling* (SigV4, no
static credentials) and admits the workload to the service-entry API as a
whole — a **one-time grant when a workload onboards**. *Which channels* that
workload may call is decided in the platform, by each channel's caller
allowlist, so day-2 operations (bind another workload, revoke one, add a
channel) never involve the ops team or an IAM change.

The platform holds no IAM write permission at all — an admin downloads this
runbook from the Channels page and hands it to whoever owns the workload's
role (the EKS Pod Identity association, in the primary case). Steps 0–4 are
one-time per workload/VPC; re-running them for another channel is never
needed.
"""

import json

from app.config import settings

POLICY_PLACEHOLDER = "<service-api-arn-base — deploy AgentPlatformPortal to fill this in>"
URL_PLACEHOLDER = "<service-api-url — deploy AgentPlatformPortal to fill this in>"


def _policy(arn_base: str) -> dict:
    """One API-wide grant per workload. Channel-level authorization is NOT
    in IAM — it is the channel's caller allowlist, enforced by the backend
    on every submit (and invocation polling is caller-scoped)."""
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "SubmitToAllowlistedChannels",
                "Effect": "Allow",
                "Action": "execute-api:Invoke",
                "Resource": f"{arn_base}/POST/service/v1/channels/*/invocations",
            },
            {
                "Sid": "PollOwnInvocations",
                "Effect": "Allow",
                "Action": "execute-api:Invoke",
                # invocation records are additionally caller-scoped in the
                # backend: a role can only read invocations it submitted
                "Resource": f"{arn_base}/GET/service/v1/invocations/*",
            },
        ],
    }


TRUST_POLICY = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Principal": {"Service": "pods.eks.amazonaws.com"},
            "Action": ["sts:AssumeRole", "sts:TagSession"],
        }
    ],
}

WORKLOAD_POLICY_NAME = "agent-platform-service-entry"


def render_sop(channel: dict) -> str:
    channel_id = channel["id"]
    name = channel.get("name", channel_id)
    arn_base = settings.service_api_arn_base or POLICY_PLACEHOLDER
    api_url = (settings.service_api_url or URL_PLACEHOLDER).rstrip("/")
    policy_json = json.dumps(_policy(arn_base), indent=2)
    trust_json = json.dumps(TRUST_POLICY, indent=2)
    allowlist = channel.get("allowed_caller_arns") or []
    allowlist_note = (
        "This channel is bound to the following caller roles — only they can "
        "submit to it, no matter who else holds the API grant:\n\n"
        + "\n".join(f"- `{a}`" for a in allowlist)
        + "\n\nTo bind a different workload, the platform admin edits the "
        "channel's allowlist on the Channels page — no IAM change involved."
    )

    return f"""# SOP — Onboard a workload to the agent-platform service entry

Requested for channel “{name}” (`{channel_id}`) · Auth: **AWS IAM (SigV4)** · Contract: **submit / poll (async)**

This runbook is executed by the **ops team that owns the calling workload's
IAM role**. The agent platform never modifies your roles; it only serves this
document.

**Run it once per workload.** The grant below admits the workload to the
service-entry API as a whole; which channels it may actually call is
controlled by each channel's caller allowlist inside the platform. If this
workload has been onboarded before, there is nothing to do here — ask the
platform admin to add the role to the channel's allowlist.

{allowlist_note}

## 0. Network prerequisite — the API is private (once per VPC)

The service entry is a **PRIVATE API Gateway**: it is unreachable from the
internet and only accepts traffic arriving through an `execute-api`
**interface VPC endpoint**. Your workload's VPC needs one (shared by every
workload and channel):

```bash
aws ec2 create-vpc-endpoint \\
  --vpc-id <workload-vpc> \\
  --vpc-endpoint-type Interface \\
  --service-name com.amazonaws.{settings.aws_region}.execute-api \\
  --subnet-ids <private-subnet-ids> \\
  --security-group-ids <sg-allowing-443-from-the-vpc> \\
  --no-private-dns-enabled
```

Pick the URL form to match your private-DNS choice:

- **Private DNS enabled** — call the plain URL below. Caution: enabling it
  makes *every* `execute-api` hostname in that VPC resolve privately, which
  breaks calls to any PUBLIC API Gateway APIs from the same VPC. Only
  enable it on a VPC you fully control.
- **Private DNS disabled** (safe default for shared VPCs) — send your
  endpoint ID to the platform admin so they can **associate it with the
  API** (`service_api_allowed_vpces` in the platform's deployment context);
  the association publishes a DNS alias and you call the endpoint-specific
  form `https://<api-id>-<vpce-id>.execute-api.{settings.aws_region}.amazonaws.com/svc`
  (same SigV4 signing, service `execute-api`). Without the association
  that hostname does not resolve.

## 1. Create the service-entry policy (once per account)

```bash
aws iam create-policy \\
  --policy-name {WORKLOAD_POLICY_NAME} \\
  --policy-document '{{}}'   # use the JSON below
```

```json
{policy_json}
```

## 2. The workload role (EKS Pod Identity)

EKS Pod Identity is the required mechanism for pods (not IRSA). If the
workload already has a Pod Identity role, just attach the policy from step 1
and skip to step 5. Otherwise create the role with the EKS Pod Identity trust
policy:

```json
{trust_json}
```

```bash
aws iam create-role --role-name <workload>-agent-caller \\
  --assume-role-policy-document file://trust.json
aws iam attach-role-policy --role-name <workload>-agent-caller \\
  --policy-arn arn:aws:iam::<account-id>:policy/{WORKLOAD_POLICY_NAME}
```

Then send the role ARN to the platform admin — the channel's caller
allowlist must include it before any call succeeds.

## 3. Ensure the Pod Identity agent add-on (once per cluster)

```bash
aws eks create-addon --cluster-name <cluster> --addon-name eks-pod-identity-agent
kubectl get pods -n kube-system | grep eks-pod-identity-agent   # should be Running
```

## 4. Associate the role with the workload's service account

```bash
aws eks create-pod-identity-association \\
  --cluster-name <cluster> \\
  --namespace <namespace> \\
  --service-account <service-account> \\
  --role-arn arn:aws:iam::<account-id>:role/<workload>-agent-caller
```

Restart the workload's pods afterwards — credentials are injected at pod
start. Any recent AWS SDK picks them up automatically (the agent sets the
container credential environment variables).

## 5. Call the channel (submit, then poll)

Base URL: `{api_url}` — substitute the endpoint-specific form from step 0
when your VPC endpoint has private DNS disabled.

```python
import time

import requests
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.session import Session

BASE = "{api_url}"
CHANNEL = "{channel_id}"


def sigv4_call(method: str, url: str, body: str = "") -> requests.Response:
    session = Session()  # Pod Identity credentials, resolved automatically
    creds = session.get_credentials()
    region = session.get_config_variable("region") or "{settings.aws_region}"
    req = AWSRequest(method=method, url=url, data=body,
                     headers={{"Content-Type": "application/json"}})
    SigV4Auth(creds, "execute-api", region).add_auth(req)
    return requests.request(method, url, data=body, headers=dict(req.headers))


# 1) submit — returns 202 immediately; the agent runs asynchronously
resp = sigv4_call(
    "POST", f"{{BASE}}/service/v1/channels/{{CHANNEL}}/invocations",
    body='{{"message": "hello from the pod", "conversation_id": "order-1234"}}',
)
resp.raise_for_status()
inv = resp.json()["invocation_id"]

# 2) poll until the run finishes
while True:
    record = sigv4_call("GET", f"{{BASE}}/service/v1/invocations/{{inv}}").json()
    if record["status"] in ("succeeded", "failed"):
        break
    time.sleep(5)
print(record["status"], record.get("result") or record.get("error"))
```

Reuse the same `conversation_id` across calls to keep the agent's session
(and, when the target agent has memory enabled, its recent-conversation
replay) continuous.

### Optional — robot identity (SSO)

If the target agent uses identity-forwarding tools, your workload also needs
a service-account identity in the IdP. Request a **client-credentials**
service account from the IdP admin, fetch a token at call time and send it as
the `x-robot-token` header on the submit request (SigV4 owns
`Authorization`, so the robot token rides its own header):

```python
tok = requests.post(f"{{IDP}}/protocol/openid-connect/token", data={{
    "grant_type": "client_credentials",
    "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
}}).json()["access_token"]
# then add {{"x-robot-token": tok}} to the submit request's headers
```

The platform verifies this token against its IdP before use; downstream APIs
authorize your workload by the service account's group claims.

## 6. Verify

From inside a pod using the service account:

```bash
aws sts get-caller-identity          # should show the workload role
```

Then run the snippet from step 5 — the first call returns 202 with an
`invocation_id`, and the poll converges to `succeeded`. A
`403 caller is not on this channel's allowlist` means the IAM side is done
but the platform admin has not added your role to this channel yet.

## Revoke / rollback

- **Unbind from this channel** — platform admin removes the role from the
  channel's allowlist (or disables the channel). No IAM change.
- **Offboard the workload entirely**:

```bash
aws eks delete-pod-identity-association --cluster-name <cluster> --association-id <id>
aws iam detach-role-policy --role-name <workload>-agent-caller \\
  --policy-arn arn:aws:iam::<account-id>:policy/{WORKLOAD_POLICY_NAME}
```
"""
