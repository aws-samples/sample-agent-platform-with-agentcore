"""stdio → signed streamable-HTTP proxy for a customer-owned MCP hub.

The Claude Agent SDK can attach an HTTP MCP server with *static* headers, but
a hub that authenticates applications with MCPHUB-HMAC-SHA256 needs a fresh
signature per request (timestamp, nonce and the body hash are all signed). So
the kernel attaches the hub as a local stdio server instead — this process —
and every JSON-RPC message is re-sent as one signed POST: the same shape as
mcp-proxy-for-aws (stdio → SigV4), with the customer's HMAC scheme in place
of SigV4.

The message bytes are forwarded verbatim (no re-serialization), so the body
hash the hub verifies is computed over exactly what the CLI produced.

Configuration (environment, set per attachment by the kernel):
    MCPHUB_URL                  hub MCP endpoint (required)
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
    MCPHUB_CREDENTIALS_SECRET   Secrets Manager secret holding this agent's
                                {"access_key": …, "secret_key": …}
    MCPHUB_ACCESS_KEY /         direct credentials — local development only,
    MCPHUB_SECRET_KEY           used when no credentials secret is named

The access/secret pair names the *published agent* as the hub's Actor; it is
scoped to that one agent and revocable by deleting/rotating its secret. The
values live in this process's environment inside a single-session microVM —
an acceptable perimeter for an agent-scoped credential, unlike the
platform-wide LLM gateway key (which is why that one gets llm_shim instead).
"""

import concurrent.futures
import json
import os
import sys
import threading
import urllib.error
import urllib.request

from mcphub_hmac import McpHubHmacSignature

REQUEST_TIMEOUT_S = 300
MAX_WORKERS = 8


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


class HubProxy:
    def __init__(
        self,
        url: str,
        access_key: str,
        secret_key: str,
        sso_token: str,
        sso_token_file: str = "",
    ) -> None:
        self.url = url
        self.access_key = access_key
        self.secret_key = secret_key
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
        headers = McpHubHmacSignature.sign(
            method="POST",
            url=self.url,
            raw_body=raw_body,
            sso_token=self._current_token(),
            content_type="application/json",
            access_key=self.access_key,
            secret_key=self.secret_key,
        )
        headers["Accept"] = "application/json, text/event-stream"
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


def main() -> int:
    url = os.environ.get("MCPHUB_URL", "")
    sso_token = os.environ.get("MCPHUB_SSO_TOKEN", "")
    sso_token_file = os.environ.get("MCPHUB_SSO_TOKEN_FILE", "")
    try:
        access_key, secret_key = load_credentials()
    except Exception as exc:  # noqa: BLE001 — startup failure, reason to stderr
        log(f"could not load hub credentials: {exc}")
        return 2
    missing = [
        name
        for name, value in (
            ("MCPHUB_URL", url),
            ("MCPHUB_SSO_TOKEN(_FILE)", sso_token or sso_token_file),
            ("access_key", access_key),
            ("secret_key", secret_key),
        )
        if not value
    ]
    if missing:
        log(f"missing configuration: {', '.join(missing)} — refusing to start")
        return 2
    # No credential-derived value in logs — the hub names the verified actor
    # in its own log line, which is where per-application audit belongs.
    log(f"forwarding to {url}")
    HubProxy(url, access_key, secret_key, sso_token, sso_token_file).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
