"""Demo workload: an EKS pod calling an agent channel the production way.

What this exercises, in one loop iteration:

1. **EKS Pod Identity** — boto3 resolves credentials injected by the
   eks-pod-identity-agent; no static keys, no IRSA annotations.
2. **SigV4 service entry** — the pod signs requests with its own role and
   calls the API Gateway front door (submit → poll). The IAM policy from the
   channel's SOP is the only grant it holds.
3. **Robot SSO identity (path A)** — the pod fetches a client-credentials
   token from the IdP with credentials it holds itself (a K8s secret here)
   and sends it as ``x-robot-token``; downstream identity-aware tools see the
   robot's group claims.
4. **Conversation continuity** — a fixed ``conversation_id`` keeps the agent
   session warm, and the recent-turns memory replay survives microVM
   recycling between iterations.
"""

import json
import os
import random
import string
import time

import requests
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.session import Session

BASE = os.environ["SERVICE_API_URL"].rstrip("/")
CHANNEL = os.environ["CHANNEL_ID"]
REGION = os.environ.get("AWS_REGION", "ap-northeast-1")
CONVERSATION = os.environ.get("CONVERSATION_ID", "eks-demo-pod")
INTERVAL_S = int(os.environ.get("INTERVAL_S", "300"))

IDP_TOKEN_URL = os.environ.get("IDP_TOKEN_URL", "")
ROBOT_CLIENT_ID = os.environ.get("ROBOT_CLIENT_ID", "")
ROBOT_CLIENT_SECRET = os.environ.get("ROBOT_CLIENT_SECRET", "")


def sigv4(method: str, url: str, body: str = "", headers: dict | None = None) -> requests.Response:
    session = Session()  # Pod Identity credentials, resolved automatically
    creds = session.get_credentials()
    if creds is None:
        raise RuntimeError("no AWS credentials — is the Pod Identity association in place?")
    req = AWSRequest(method=method, url=url, data=body,
                     headers={"Content-Type": "application/json", **(headers or {})})
    SigV4Auth(creds, "execute-api", REGION).add_auth(req)
    return requests.request(method, url, data=body, headers=dict(req.headers), timeout=30)


def robot_token() -> str:
    """Client-credentials grant with credentials the pod holds itself
    (path A) — the platform never stores them."""
    if not (IDP_TOKEN_URL and ROBOT_CLIENT_ID and ROBOT_CLIENT_SECRET):
        return ""
    resp = requests.post(
        IDP_TOKEN_URL,
        data={
            "grant_type": "client_credentials",
            "client_id": ROBOT_CLIENT_ID,
            "client_secret": ROBOT_CLIENT_SECRET,
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def run_once(n: int) -> None:
    whoami = Session().create_client("sts", region_name=REGION).get_caller_identity()
    print(f"[{n}] caller: {whoami['Arn']}", flush=True)

    headers = {}
    tok = robot_token()
    if tok:
        headers["x-robot-token"] = tok
        print(f"[{n}] robot token acquired ({ROBOT_CLIENT_ID})", flush=True)

    # A random per-iteration code makes the continuity check falsifiable:
    # the previous code cannot be guessed or derived — the agent can only
    # answer it from the replayed conversation (memory), never from
    # arithmetic on the check-in number.
    nonce = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
    print(f"[{n}] my verification code: {nonce}", flush=True)
    body = json.dumps({
        "message": f"Check-in #{n} from the order-service pod. My verification code "
                   f"this time is {nonce}. First tell me the verification code from "
                   f"the previous check-in (answer NONE if there was no previous one), "
                   f"then acknowledge this check-in.",
        "conversation_id": CONVERSATION,
    })
    resp = sigv4("POST", f"{BASE}/service/v1/channels/{CHANNEL}/invocations", body, headers)
    print(f"[{n}] submit → {resp.status_code} {resp.text[:200]}", flush=True)
    resp.raise_for_status()
    inv = resp.json()["invocation_id"]

    deadline = time.time() + 600
    while time.time() < deadline:
        time.sleep(10)
        record = sigv4("GET", f"{BASE}/service/v1/invocations/{inv}").json()
        if record.get("status") in ("succeeded", "failed"):
            print(f"[{n}] {record['status']}: "
                  f"{(record.get('result') or record.get('error'))[:400]}", flush=True)
            return
        print(f"[{n}] … {record.get('status')}", flush=True)
    print(f"[{n}] timed out waiting for the invocation", flush=True)


def main() -> None:
    n = 0
    while True:
        n += 1
        try:
            run_once(n)
        except Exception as e:  # noqa: BLE001 — a demo loop should survive blips
            print(f"[{n}] error: {e}", flush=True)
        time.sleep(INTERVAL_S)


if __name__ == "__main__":
    main()
