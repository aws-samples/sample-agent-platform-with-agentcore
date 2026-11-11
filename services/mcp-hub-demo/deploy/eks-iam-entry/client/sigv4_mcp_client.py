"""A minimal MCP client that authenticates to the hub with its pod's IAM role.

No key material: the credentials come from botocore's default chain, which
under IRSA reads the projected service-account token
(AWS_WEB_IDENTITY_TOKEN_FILE + AWS_ROLE_ARN, injected by EKS) and calls
sts:AssumeRoleWithWebIdentity, refreshing before expiry. Every request is
SigV4-signed for execute-api, body included.

It speaks MCP streamable HTTP by hand (one JSON-RPC message per POST) so the
signing is visible, and needs nothing beyond boto3 and the standard library.
In an application, put the same signing in an httpx Auth and hand that
client to the MCP SDK's streamable_http_client.

Usage (inside the frontend pod):
    python sigv4_mcp_client.py https://<api-id>.execute-api.<region>.amazonaws.com/mcp/mcp

Prints one JSON line per step and, last, a RESULT line:
    RESULT {"ok": true, "status": 200, "actor": "...", "caller_arn": "...", "tools": [...]}
Exit code 0 only when the whole exchange succeeded.
"""

from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

PROTOCOL_VERSION = "2025-06-18"


def region_of(url: str) -> str:
    m = re.search(r"\.execute-api\.([a-z0-9-]+)\.amazonaws\.com", url)
    if not m:
        raise SystemExit("the URL must be an execute-api invoke URL")
    return m.group(1)


class SigV4Signer:
    def __init__(self, region: str) -> None:
        self.session = boto3.Session()
        self.region = region

    def headers(self, method: str, url: str, body: bytes, extra: dict[str, str]) -> dict[str, str]:
        creds = self.session.get_credentials()  # refreshable: re-read per request
        if creds is None:
            raise SystemExit("no AWS credentials in the default chain (is the pod's service account annotated?)")
        req = AWSRequest(method=method, url=url, data=body, headers=extra)
        SigV4Auth(creds.get_frozen_credentials(), "execute-api", self.region).add_auth(req)
        return dict(req.headers.items())


@dataclass
class Response:
    status: int
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)  # lower-cased names


def post(url: str, body: bytes, headers: dict[str, str], timeout: float = 30.0) -> Response:
    """One POST; an HTTP error status is a Response, not an exception."""
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return Response(r.status, r.read(), {k.lower(): v for k, v in r.headers.items()})
    except urllib.error.HTTPError as e:
        return Response(e.code, e.read(), {k.lower(): v for k, v in e.headers.items()})


def parse(resp: Response) -> dict | None:
    # A body is parsed whatever the status: a 202 that carries one is not the
    # empty acknowledgement it claims to be.
    if not resp.body:
        return None
    text = resp.body.decode("utf-8", errors="replace")
    if resp.headers.get("content-type", "").startswith("text/event-stream"):
        for line in text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:].strip())
        return None
    return json.loads(text)


def main(url: str) -> int:
    signer = SigV4Signer(region_of(url))
    identity = boto3.client("sts", region_name=signer.region).get_caller_identity()["Arn"]
    print(json.dumps({"step": "local-identity", "arn": identity}))

    session_id: str | None = None
    result: dict = {"ok": False}

    raw_body = {"last": b""}  # the bytes of the last response, before parsing

    def rpc(msg: dict) -> tuple[int, dict | None]:
        nonlocal session_id
        body = json.dumps(msg, separators=(",", ":")).encode()
        base = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": PROTOCOL_VERSION}
        if session_id:
            base["Mcp-Session-Id"] = session_id
        resp = post(url, body, signer.headers("POST", url, body, base))
        raw_body["last"] = resp.body
        session_id = resp.headers.get("mcp-session-id", session_id)
        out = None
        try:
            out = parse(resp)
        except ValueError:  # json.JSONDecodeError is a ValueError
            out = {"raw": resp.body[:300].decode("utf-8", errors="replace")}
        print(json.dumps({"step": msg.get("method"), "status": resp.status, "body": out})[:2000])
        return resp.status, out

    def fail(step: str, status: int) -> int:
        # Every step must succeed for ok=true: the first failing one is
        # reported and ends the exchange.
        result.update(ok=False, failed_step=step, status=status)
        print("RESULT " + json.dumps(result))
        return 1

    def answered(status: int, out: dict | None, rid: int) -> dict | None:
        """The JSON-RPC result of a request, or None on any HTTP/JSON-RPC error."""
        if status != 200 or not isinstance(out, dict) or out.get("id") != rid or "error" in out:
            return None
        res = out.get("result")
        return res if isinstance(res, dict) else None

    status, init = rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": PROTOCOL_VERSION, "capabilities": {},
        "clientInfo": {"name": "sigv4-mcp-client", "version": "0.1"}}})
    result["status"] = status
    if answered(status, init, 1) is None:
        return fail("initialize", status)

    # A notification has no response: 202 or 200 with no body is the success.
    # Any body at all - a JSON-RPC error, a non-JSON page - fails the step.
    # Judged on the raw bytes: a body that parses to nothing (the literal
    # "null") is still a body.
    status, _ = rpc({"jsonrpc": "2.0", "method": "notifications/initialized"})
    if status not in (200, 202) or raw_body["last"].strip():
        return fail("notifications/initialized", status)

    status, listed = rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    res = answered(status, listed, 2)
    if res is None or not isinstance(res.get("tools"), list) or not all(
            isinstance(t, dict) and isinstance(t.get("name"), str) for t in res["tools"]):
        return fail("tools/list", status)
    result["tools"] = [t.get("name") for t in res["tools"]]

    status, called = rpc({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                          "params": {"name": "hub__whoami", "arguments": {}}})
    res = answered(status, called, 3)
    if res is None or res.get("isError") or not res.get("content"):
        return fail("tools/call hub__whoami", status)
    try:
        who = json.loads(res["content"][0]["text"])
    except (KeyError, IndexError, TypeError, ValueError):
        return fail("tools/call hub__whoami", status)
    if not isinstance(who, dict) or not isinstance(who.get("actor"), str) or not isinstance(who.get("caller_arn"), str):
        return fail("tools/call hub__whoami", status)
    result.update(ok=True, status=status, actor=who.get("actor"), caller_arn=who.get("caller_arn"),
                  trust=who.get("trust"), user=who.get("user"))

    print("RESULT " + json.dumps(result))
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    sys.exit(main(sys.argv[1]))
