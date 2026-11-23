"""A minimal MCP client for an Amazon Bedrock AgentCore gateway.

Two ways to authenticate, matching the two gateways of this deployment:

  --auth sigv4    an application with no user. Credentials come from
                  botocore's default chain - under IRSA the pod's projected
                  service-account token, exchanged with
                  sts:AssumeRoleWithWebIdentity and refreshed before expiry.
                  Every request is SigV4-signed for bedrock-agentcore, body
                  included. No key material anywhere.
  --auth bearer   a signed-in user. The access token is read from stdin (one
                  line), never from the command line.
  --auth none     no credentials at all (negative tests).

It speaks MCP streamable HTTP by hand (one JSON-RPC message per POST) so the
authentication is visible, and needs only boto3 (sigv4) and the standard
library. It lists the tools, then calls the hub's whoami tool, and reports
what happened without judging it: a caller the gateway's Cedar policy does
not permit is expected to see no tool and have its call refused.

Usage:
    python gateway_mcp_client.py --auth sigv4 https://<gateway-id>.gateway.bedrock-agentcore.<region>.amazonaws.com/mcp

Prints one JSON line per step and, last, a RESULT line:
    RESULT {"initialize": 200, "tools": [...], "call": "ok"|"denied"|"error"|"skipped", "whoami": {...}|null, "detail": "..."}
Exit code 0 when the tool call succeeded, 3 when the gateway's policy refused
it (and only then: see policy_refusal), 1 when the exchange itself failed (including a request that
got no HTTP answer: the RESULT line then carries transport_error; or an
answer that does not carry the request's id: id_mismatch). The client sends
each request to exactly the URL it was given and follows no redirect.
"""

from __future__ import annotations

import argparse
import http.client
import json
import re
import sys
import urllib.error
import urllib.request

PROTOCOL_VERSION = "2025-06-18"  # what the client offers; the server's answer to initialize is what gets used
TOOL = "hub___hub__whoami"  # <target>___<tool>
# What an AgentCore gateway answers when its policy engine refuses a tool
# call: HTTP 200, a JSON-RPC error with this code and a message that says so
# (observed: "Tool Execution Denied: Tool call not allowed due to policy
# enforcement [...]"). Any other error - an unknown tool is -32602, an
# internal error -32603 - is a failure, not a refusal.
POLICY_DENIED_CODE = -32002
POLICY_DENIED_TEXT = "policy enforcement"


def policy_refusal(status: int, out) -> bool:
    """Whether this answer to tools/call is the gateway's policy refusing it."""
    err = out.get("error") if isinstance(out, dict) else None
    return (status == 200 and isinstance(err, dict) and err.get("code") == POLICY_DENIED_CODE
            and isinstance(err.get("message"), str) and POLICY_DENIED_TEXT in err["message"])


def region_of(url: str) -> str:
    m = re.fullmatch(r"https://[a-z0-9-]+\.gateway\.bedrock-agentcore\.([a-z0-9-]+)\.amazonaws\.com/mcp", url)
    if not m:
        raise SystemExit("the URL must be an AgentCore gateway MCP endpoint")
    return m.group(1)


def local_identity(region: str) -> str:
    """The caller's ARN, for the log only: which role the pod is running as.

    Diagnostic, so a failure here is reported and the exchange goes on - the
    gateway's answer is what decides the outcome, not this call.
    """
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError

    try:
        return boto3.client("sts", region_name=region).get_caller_identity()["Arn"]
    except (BotoCoreError, ClientError) as exc:
        return f"unknown ({type(exc).__name__})"


class Auth:
    def __init__(self, mode: str, url: str) -> None:
        self.mode = mode
        self.token = ""
        if mode == "bearer":
            self.token = sys.stdin.readline().strip()
            if not self.token:
                raise SystemExit("--auth bearer reads the access token from stdin; got nothing")
        elif mode == "sigv4":
            import boto3

            self.region = region_of(url)
            self.session = boto3.Session()
            print(json.dumps({"step": "local-identity", "arn": local_identity(self.region)}))

    def headers(self, url: str, body: bytes, base: dict[str, str]) -> dict[str, str]:
        if self.mode == "bearer":
            return {**base, "Authorization": f"Bearer {self.token}"}
        if self.mode == "sigv4":
            from botocore.auth import SigV4Auth
            from botocore.awsrequest import AWSRequest

            creds = self.session.get_credentials()  # refreshable: re-read per request
            if creds is None:
                raise SystemExit("no AWS credentials in the default chain (is the pod's service account annotated?)")
            req = AWSRequest(method="POST", url=url, data=body, headers=base)
            SigV4Auth(creds.get_frozen_credentials(), "bedrock-agentcore", self.region).add_auth(req)
            return dict(req.headers.items())
        return dict(base)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect.

    urllib follows a 301/302/303 by repeating the request at the Location it
    was given, Authorization header included. The URL check above confines
    the credential to an AgentCore gateway; a redirect would carry it to
    wherever the answer points. A gateway does not redirect, so a 3xx is
    returned to the caller as the answer it is, and nothing is sent again.
    """

    def redirect_request(self, *args, **kwargs):
        return None


# Built once, with no proxy lookup from the environment either: the request
# goes to the URL it was given and nowhere else.
_OPENER = urllib.request.build_opener(_NoRedirect, urllib.request.ProxyHandler({}))


def post(url: str, body: bytes, headers: dict[str, str], timeout: float = 60.0) -> tuple[int, bytes, dict[str, str]]:
    """One POST to exactly this URL; an HTTP error status (a redirect too) is a response, not an exception."""
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            return r.status, r.read(), {k.lower(): v for k, v in r.headers.items()}
    except urllib.error.HTTPError as e:
        return e.code, e.read(), {k.lower(): v for k, v in e.headers.items()}


def parse(body: bytes, headers: dict[str, str]):
    if not body:
        return None
    text = body.decode("utf-8", errors="replace")
    try:
        if headers.get("content-type", "").startswith("text/event-stream"):
            for line in text.splitlines():
                if line.startswith("data:"):
                    return json.loads(line[5:].strip())
            return None
        return json.loads(text)
    except ValueError:
        return {"raw": text[:300]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--auth", choices=["sigv4", "bearer", "none"], required=True)
    ap.add_argument("url")
    args = ap.parse_args()
    region_of(args.url)
    auth = Auth(args.auth, args.url)
    result: dict = {"initialize": None, "tools": None, "call": "skipped", "whoami": None, "detail": ""}
    session_id: str | None = None
    version = PROTOCOL_VERSION

    def rpc(msg: dict) -> tuple[int, dict | None, dict[str, str]]:
        nonlocal session_id
        body = json.dumps(msg, separators=(",", ":")).encode()
        base = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": version}
        if session_id:
            base["Mcp-Session-Id"] = session_id
        try:
            status, raw, headers = post(args.url, body, auth.headers(args.url, body, base))
        # no HTTP answer, or a broken one (refused, timed out, reset, no DNS, a body cut
        # short): a failed step with a RESULT line, never a traceback verify.sh cannot read
        except (OSError, http.client.HTTPException) as e:
            result["transport_error"] = f"{type(getattr(e, 'reason', e)).__name__}: {getattr(e, 'reason', e)}"[:300]
            print(json.dumps({"step": msg.get("method"), "status": 0, "transport_error": result["transport_error"]}))
            return 0, None, {}
        session_id = headers.get("mcp-session-id", session_id)
        out = parse(raw, headers)
        print(json.dumps({"step": msg.get("method"), "status": status, "body": out})[:1500])
        out = out if isinstance(out, dict) else None
        # An answer is evidence only about the request it answers. A JSON-RPC
        # response must carry the request's id (exactly: 3 is not "3", not 3.0,
        # not true); one that does not - another call's result or refusal,
        # replayed or misrouted - says nothing about this request.
        #
        # What is dropped is the body, not the HTTP answer. A gateway that
        # rejects the credential answers 401 before it reads the JSON-RPC
        # request, with an id of its own (an AgentCore gateway with a JWT
        # authorizer answers id 0); that status is still the gateway's answer
        # to this HTTP request. So: on a 200, where the body is the only
        # evidence, a mismatched id fails the step (status 0); on any other
        # status the status and headers stand and the body is ignored.
        if "id" in msg and out is not None and ("result" in out or "error" in out):
            got = out.get("id")
            if type(got) is not type(msg["id"]) or got != msg["id"]:
                result["id_mismatch"] = f"{msg.get('method')}: sent id {msg['id']!r}, answer carries {got!r}"
                return (0 if status == 200 else status), None, headers
        return status, out, headers

    def finish(code: int) -> int:
        print("RESULT " + json.dumps(result))
        return code

    status, out, headers = rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": PROTOCOL_VERSION, "capabilities": {},
        "clientInfo": {"name": "gateway-mcp-client", "version": "1"}}})
    result["initialize"] = status
    if status != 200 or not out or not isinstance(out.get("result"), dict) or out.get("error") is not None:
        result["detail"] = headers.get("www-authenticate", "") or json.dumps(out)[:300]
        return finish(1)
    # Version negotiation: the server answers with the version it will speak
    # (a gateway may support older ones only), and every later request must
    # carry that one, not the one the client offered.
    negotiated = out["result"].get("protocolVersion")
    if isinstance(negotiated, str) and negotiated:
        version = negotiated
    # A step like the others: a gateway that does not accept the notification
    # has not completed the handshake, whatever it answers afterwards.
    status, out, _ = rpc({"jsonrpc": "2.0", "method": "notifications/initialized"})
    if status not in (200, 202) or (out is not None and out.get("error") is not None):
        result["detail"] = f"notifications/initialized: status {status}"
        return finish(1)

    status, out, _ = rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    listed = (out or {}).get("result") if status == 200 and (out or {}).get("error") is None else None
    tools = listed.get("tools") if isinstance(listed, dict) else None
    if not isinstance(tools, list):
        result["detail"] = f"tools/list: status {status}"
        return finish(1)
    result["tools"] = sorted(str(t.get("name")) for t in tools if isinstance(t, dict))

    # Called whether or not it was listed: a policy must refuse the call
    # itself, not merely hide the tool.
    status, out, _ = rpc({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                          "params": {"name": TOOL, "arguments": {}}})
    err = (out or {}).get("error")
    res = (out or {}).get("result") if status == 200 else None
    if err is not None or not isinstance(res, dict) or res.get("isError"):
        # "denied" is a claim about the gateway's policy, and verify.sh takes it
        # as proof that Cedar refused the caller. So it is reported only for the
        # answer a policy refusal actually is (policy_refusal above). Every other
        # failure - no answer, another status, an internal error, an unknown
        # tool, a tool that ran and failed - is an error.
        refused = policy_refusal(status, out)
        result["call"] = "denied" if refused else "error"
        result["detail"] = json.dumps(err or res or {"status": status})[:400]
        return finish(3 if refused else 1)
    try:
        whoami = json.loads(res["content"][0]["text"])
    except (KeyError, IndexError, TypeError, ValueError):
        result.update(call="error", detail="tool result is not the hub's JSON")
        return finish(1)
    if not isinstance(whoami, dict) or TOOL not in result["tools"]:
        result.update(call="error", detail="tool answered but was not listed, or its result is not an object")
        return finish(1)
    result.update(call="ok", whoami=whoami)
    return finish(0)


if __name__ == "__main__":
    sys.exit(main())
