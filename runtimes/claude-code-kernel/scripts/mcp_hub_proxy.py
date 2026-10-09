"""stdio → authenticated streamable-HTTP proxy for a customer-owned MCP hub.

The Claude Agent SDK can attach an HTTP MCP server with *static* headers, but
a hub that authenticates applications needs a fresh signature per request
(timestamp, nonce / date, body hash are all signed). So the kernel attaches
the hub as a local stdio server instead — this process — and every JSON-RPC
message is re-sent as one signed POST: the same shape as mcp-proxy-for-aws
(stdio → SigV4), with the application-auth scheme the deployment uses.

The message bytes are forwarded verbatim (no re-serialization), so the body
hash the far end verifies is computed over exactly what the CLI produced.

Two application-auth schemes (MCPHUB_AUTH):

  hmac (default)  MCPHUB-HMAC-SHA256 straight to the hub: the published agent
                  is the hub's Actor, named by an access/secret key pair in
                  Secrets Manager.
  iam             the hub sits behind the platform's private API Gateway
                  (AWS_IAM). Every POST is SigV4-signed (service execute-api)
                  with short-lived credentials of the hub *caller role* that
                  the platform backend minted for this attachment: a session
                  named after the actor (agent-<id> for a published agent,
                  dev-workbench for workbench / Debug console). This
                  container cannot assume that role itself — the backend, the
                  one component that knows which agent an invocation belongs
                  to, chooses the session name — so a kernel cannot present
                  another agent's identity. The gateway forwards the
                  authenticated identity to the hub, which reads the actor from
                  the session name. No key pair exists anywhere.

Configuration (environment, set per attachment by the kernel):
    MCPHUB_URL                  hub MCP endpoint (hmac: the hub itself; iam:
                                the entry API's invoke URL) — required
    MCPHUB_SSO_TOKEN            the acting user's SSO access token (forwarded
                                as X-MCPHUB-SSO-TOKEN; the hub resolves
                                identity and permissions from it)
    MCPHUB_SSO_TOKEN_FILE       alternative to MCPHUB_SSO_TOKEN: a file whose
                                content is the token, re-read on every request.
                                The interactive kernel uses this so the token
                                never lands in /workspace/.mcp.json (which
                                syncs to S3) and so a session re-warmup can
                                refresh the token without restarting this
                                proxy. One of the two is required.
    MCPHUB_AUTH                 hmac | iam (default hmac)

  hmac:
    MCPHUB_CREDENTIALS_SECRET   Secrets Manager secret holding this agent's
                                {"access_key": …, "secret_key": …}
    MCPHUB_ACCESS_KEY /         direct credentials — local development only,
    MCPHUB_SECRET_KEY           used when no credentials secret is named

  iam:
    MCPHUB_ACTOR                the actor this attachment acts as (agent-<id>
                                | dev-workbench) — informational here (the
                                session the credentials carry is what the hub
                                sees), checked for shape, required
    MCPHUB_CALLER_CREDENTIALS   JSON {"access_key_id", "secret_access_key",
                                "session_token", "expiration"}: the caller-role
                                session the backend minted (headless kernel)
    MCPHUB_CALLER_CREDENTIALS_FILE
                                alternative: a file holding that JSON, re-read
                                on every request (interactive kernel: never in
                                .mcp.json, refreshed in place by a re-warmup).
                                One of the two is required.

Credential-derived values never reach the logs: the hub names the verified
actor in its own log line, which is where per-application audit belongs.
"""

import concurrent.futures
import datetime
import json
import os
import re
import sys
import threading
import urllib.error
import urllib.request

from mcphub_hmac import McpHubHmacSignature

REQUEST_TIMEOUT_S = 300
MAX_WORKERS = 8
ACTOR_RE = re.compile(r"^(agent-[A-Za-z0-9_.-]{1,64}|dev-workbench)$")
CREDENTIAL_KEYS = ("access_key_id", "secret_access_key", "session_token")


def log(msg: str) -> None:
    print(f"mcp-hub-proxy: {msg}", file=sys.stderr, flush=True)


def load_credentials() -> tuple[str, str]:
    secret_name = os.environ.get("MCPHUB_CREDENTIALS_SECRET", "")
    if secret_name:
        import boto3

        sm = boto3.client(
            "secretsmanager", region_name=os.environ.get("AWS_REGION", "us-east-1")
        )
        data = json.loads(sm.get_secret_value(SecretId=secret_name)["SecretString"])
        return str(data.get("access_key", "")), str(data.get("secret_key", ""))
    return os.environ.get("MCPHUB_ACCESS_KEY", ""), os.environ.get("MCPHUB_SECRET_KEY", "")


class HmacSigner:
    """MCPHUB-HMAC-SHA256: the application is the Actor named by the key pair."""

    scheme = "hmac"

    def __init__(self, url: str, access_key: str, secret_key: str) -> None:
        self.url = url
        self.access_key = access_key
        self.secret_key = secret_key

    def headers(self, raw_body: bytes, sso_token: str) -> dict:
        headers = McpHubHmacSignature.sign(
            method="POST",
            url=self.url,
            raw_body=raw_body,
            sso_token=sso_token,
            content_type="application/json",
            access_key=self.access_key,
            secret_key=self.secret_key,
        )
        headers["Accept"] = "application/json, text/event-stream"
        return headers


def region_of(url: str) -> str:
    """The region to sign for: the one in an execute-api hostname, else the
    container's."""
    m = re.search(r"\.execute-api\.([a-z0-9-]+)\.amazonaws\.com", url)
    return m.group(1) if m else os.environ.get("AWS_REGION", "us-east-1")


def parse_credentials(text: str) -> dict:
    """The backend-minted caller session, validated: all three keys, non-empty."""
    data = json.loads(text)
    if not isinstance(data, dict) or any(not str(data.get(k, "")).strip() for k in CREDENTIAL_KEYS):
        raise ValueError("caller credentials must carry access_key_id, secret_access_key and session_token")
    return data


class IamSigner:
    """SigV4 (execute-api) with the caller-role session the backend minted for
    this attachment. Nothing here is a hub credential, and nothing here can
    obtain a different session: the credentials name the actor (their session
    name), the gateway verifies the signature and forwards that identity."""

    scheme = "iam"

    def __init__(self, url: str, actor: str, region: str, credentials: dict | None, credentials_file: str) -> None:
        from botocore.auth import SigV4Auth  # noqa: F401 — fail fast if botocore is missing

        self.url = url
        self.actor = actor
        self.region = region
        self._static = credentials
        self._file = credentials_file
        self._lock = threading.Lock()
        self._cached: tuple[float, dict] | None = None  # (mtime, credentials)
        self._warned_expired = False

    def _current(self) -> dict:
        if not self._file:
            return self._static
        with self._lock:
            try:
                mtime = os.stat(self._file).st_mtime
                if self._cached is None or self._cached[0] != mtime:
                    with open(self._file, encoding="utf-8") as fh:
                        self._cached = (mtime, parse_credentials(fh.read()))
                        self._warned_expired = False
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                if self._cached is None:
                    raise RuntimeError(f"caller credentials file unreadable: {exc}") from exc
                log(f"caller credentials file unreadable ({exc}); keeping the previous session")
            return self._cached[1]

    def headers(self, raw_body: bytes, sso_token: str) -> dict:
        from botocore.auth import SigV4Auth
        from botocore.awsrequest import AWSRequest
        from botocore.credentials import Credentials

        c = self._current()
        exp = str(c.get("expiration", ""))
        if exp and not self._warned_expired:
            try:
                when = datetime.datetime.fromisoformat(exp.replace("Z", "+00:00"))
                if when <= datetime.datetime.now(datetime.timezone.utc):
                    log("the caller session has expired; the platform refreshes it at the next warmup")
                    self._warned_expired = True
            except ValueError:
                pass
        req = AWSRequest(
            method="POST",
            url=self.url,
            data=raw_body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                McpHubHmacSignature.HEADER_SSO_TOKEN: sso_token,
            },
        )
        creds = Credentials(str(c["access_key_id"]), str(c["secret_access_key"]), str(c["session_token"]))
        SigV4Auth(creds, "execute-api", self.region).add_auth(req)
        return dict(req.headers.items())


class HubProxy:
    def __init__(
        self,
        url: str,
        signer,
        sso_token: str,
        sso_token_file: str = "",
    ) -> None:
        self.url = url
        self.signer = signer
        self.sso_token = sso_token
        self.sso_token_file = sso_token_file
        self._stdout_lock = threading.Lock()

    def _current_token(self) -> str:
        if self.sso_token_file:
            try:
                with open(self.sso_token_file, encoding="utf-8") as fh:
                    token = fh.read().strip()
                if token:
                    return token
                log(f"token file {self.sso_token_file} is empty")
            except OSError as exc:
                log(f"could not read token file: {exc}")
        return self.sso_token

    def _emit(self, line: str) -> None:
        with self._stdout_lock:
            sys.stdout.write(line.rstrip("\n") + "\n")
            sys.stdout.flush()

    def _post(self, raw_body: bytes) -> tuple[int, str, str]:
        """One signed POST → (status, content_type, body_text)."""
        headers = self.signer.headers(raw_body, self._current_token())
        req = urllib.request.Request(self.url, data=raw_body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_S) as resp:  # nosec B310 — fixed https/http endpoint from platform registry
                return (
                    resp.status,
                    resp.headers.get("content-type", ""),
                    resp.read().decode("utf-8", errors="replace"),
                )
        except urllib.error.HTTPError as exc:
            return (
                exc.code,
                exc.headers.get("content-type", "") if exc.headers else "",
                exc.read().decode("utf-8", errors="replace"),
            )

    def handle(self, line: str) -> None:
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            log(f"dropping non-JSON line: {line[:120]!r}")
            return
        msg_id = message.get("id")
        try:
            status, ctype, body = self._post(line.encode("utf-8"))
        except Exception as exc:  # noqa: BLE001 — surfaced to the client as a JSON-RPC error
            if msg_id is not None:
                self._emit(json.dumps({
                    "jsonrpc": "2.0", "id": msg_id,
                    "error": {"code": -32000, "message": f"hub unreachable: {exc}"},
                }))
            log(f"POST failed: {exc}")
            return

        if msg_id is None:
            # notification — the hub acknowledges with 202 and no body
            if status >= 400:
                log(f"notification rejected ({status}): {body[:200]}")
            return
        if status >= 400:
            # keep the hub's own reason (auth failures name their cause)
            self._emit(json.dumps({
                "jsonrpc": "2.0", "id": msg_id,
                "error": {"code": -32000, "message": f"hub returned {status}: {body[:300]}"},
            }))
            return
        if "text/event-stream" in ctype:
            # a streaming hub sends the response (and any interim messages)
            # as SSE data events; each event is one JSON-RPC message
            for part in body.splitlines():
                if part.startswith("data:"):
                    self._emit(part[5:].strip())
            return
        self._emit(body)

    def run(self) -> None:
        # Requests may arrive pipelined (parallel tool calls in one agent
        # turn); each is signed and sent on its own worker.
        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            for line in sys.stdin:
                line = line.strip()
                if line:
                    pool.submit(self.handle, line)


def build_signer(url: str) -> tuple[object | None, list[str]]:
    """(signer, missing-configuration names) for the configured scheme."""
    auth = os.environ.get("MCPHUB_AUTH", "hmac").strip().lower() or "hmac"
    if auth == "iam":
        actor = os.environ.get("MCPHUB_ACTOR", "").strip()
        creds_json = os.environ.get("MCPHUB_CALLER_CREDENTIALS", "").strip()
        creds_file = os.environ.get("MCPHUB_CALLER_CREDENTIALS_FILE", "").strip()
        missing = []
        if not actor:
            missing.append("MCPHUB_ACTOR")
        elif not ACTOR_RE.match(actor):
            log(f"MCPHUB_ACTOR is not an actor the hub caller role admits: {actor!r}")
            missing.append("MCPHUB_ACTOR(valid)")
        credentials = None
        if creds_json:
            try:
                credentials = parse_credentials(creds_json)
            except (ValueError, json.JSONDecodeError) as exc:
                log(f"MCPHUB_CALLER_CREDENTIALS is not a caller session: {exc}")
                missing.append("MCPHUB_CALLER_CREDENTIALS(valid)")
        elif not creds_file:
            missing.append("MCPHUB_CALLER_CREDENTIALS(_FILE)")
        if missing or not url:
            return None, missing
        return IamSigner(url, actor, region_of(url), credentials, creds_file), []
    if auth != "hmac":
        log(f"unknown MCPHUB_AUTH {auth!r}")
        return None, ["MCPHUB_AUTH(hmac|iam)"]
    access_key, secret_key = load_credentials()
    missing = [n for n, v in (("access_key", access_key), ("secret_key", secret_key)) if not v]
    if missing or not url:
        return None, missing
    return HmacSigner(url, access_key, secret_key), []


def main() -> int:
    url = os.environ.get("MCPHUB_URL", "")
    sso_token = os.environ.get("MCPHUB_SSO_TOKEN", "")
    sso_token_file = os.environ.get("MCPHUB_SSO_TOKEN_FILE", "")
    try:
        signer, missing = build_signer(url)
    except Exception as exc:  # noqa: BLE001 — startup failure, reason to stderr
        log(f"could not load hub credentials: {exc}")
        return 2
    if not url:
        missing.insert(0, "MCPHUB_URL")
    if not (sso_token or sso_token_file):
        missing.append("MCPHUB_SSO_TOKEN(_FILE)")
    if missing:
        log(f"missing configuration: {', '.join(missing)} — refusing to start")
        return 2
    if signer.scheme == "iam":
        log(f"forwarding to {url} through the IAM entry as {signer.actor}")
    else:
        log(f"forwarding to {url}")
    HubProxy(url, signer, sso_token, sso_token_file).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
