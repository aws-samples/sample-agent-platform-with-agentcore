"""Loopback shim keeping the model-gateway credential out of agent processes.

The Claude Agent SDK runs each query in a fresh CLI subprocess, and that
subprocess is where agent tools execute — Bash, file reads, whatever the agent
was granted. So a credential placed in the subprocess environment is a
credential any agent (or anything that talks an agent into printing its
environment) can read.

This kernel therefore never puts a gateway credential there. The platform
grant, which points at the internal ``llm-edge`` service, stays in *this*
process. The CLI is pointed at this shim and handed a token that only means
something here: a random value registered for the duration of one invocation.
Reading it out of the subprocess environment yields nothing usable — not off
this container, and not after the invocation ends.

Per-invocation registration is also what makes concurrent runs safe: two agents
routed to different backends each get their own local token, so neither can
borrow the other's grant.

Two upstream shapes are supported, decided by the grant the platform delivers:

- ``llm-edge``: a bearer token plus the session id, which the edge re-reads its
  own grant from.
- ``agentcore_gateway``: STS credentials tagged with the session id, which this
  shim SigV4-signs each request with. Claude Code cannot sign, which is exactly
  why the signing has to happen here rather than in the CLI subprocess.
"""

from __future__ import annotations

import http.client
import http.server
import json
import logging
import secrets
import threading
import urllib.parse

logger = logging.getLogger(__name__)

PORT = 8787
BASE_URL = f"http://127.0.0.1:{PORT}"

# local token -> platform grant {endpoint, session_id, token, expires_at}
_grants: dict[str, dict] = {}
_lock = threading.Lock()
_started = False

# Hop-by-hop headers, plus the caller's own Authorization: that carries the
# local token, which must not travel upstream.
_DROP_REQUEST_HEADERS = {
    "authorization",
    "x-api-key",
    "host",
    "connection",
    "keep-alive",
    "transfer-encoding",
    "upgrade",
    "proxy-authorization",
    "content-length",
    # gzip forces a compressor to buffer the whole body, which would turn a
    # token stream into one delivery at the end.
    "accept-encoding",
}
_DROP_RESPONSE_HEADERS = {
    "connection",
    "keep-alive",
    "transfer-encoding",
    "content-encoding",
    "content-length",
}

# On the SigV4 path a client-supplied x-amz-* header would either be folded
# into the signature or contradict it, so the client never gets to set one.
_SIGV4_RESERVED_PREFIX = "x-amz-"


def _sign_sigv4(grant: dict, method: str, url: str, body: bytes) -> dict:
    """Return the SigV4 headers for one request against the gateway.

    Only a minimal, deterministic header set is signed (content-type, plus the
    host and date botocore adds). Everything the caller sent is attached
    *after* signing: headers outside SignedHeaders do not participate in the
    signature, so passing Claude Code's ``anthropic-*`` and ``x-stainless-*``
    headers through cannot invalidate it.
    """
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest
    from botocore.credentials import Credentials

    creds = Credentials(
        grant["access_key_id"],
        grant["secret_access_key"],
        grant.get("session_token") or None,
    )
    req = AWSRequest(
        method=method,
        url=url,
        data=body,
        headers={"content-type": "application/json"},
    )
    SigV4Auth(
        creds,
        str(grant.get("service") or "bedrock-agentcore"),
        str(grant.get("region") or ""),
    ).add_auth(req)
    return dict(req.headers)


def register(grant: dict) -> str:
    """Register a platform grant for one invocation; returns the local token to
    put in the CLI subprocess's ANTHROPIC_AUTH_TOKEN."""
    if not grant or not grant.get("endpoint"):
        raise ValueError("gateway grant is missing endpoint")
    if not grant.get("token") and not grant.get("access_key_id"):
        raise ValueError(
            "gateway grant carries neither a bearer token (llm-edge) nor STS "
            "credentials (agentcore_gateway)"
        )
    local = secrets.token_urlsafe(24)
    with _lock:
        _grants[local] = grant
    return local


def release(local_token: str) -> None:
    """Forget a local token once its invocation is done."""
    if not local_token:
        return
    with _lock:
        _grants.pop(local_token, None)


def _grant_for(local_token: str) -> dict | None:
    with _lock:
        return _grants.get(local_token)


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "agent-platform-llm-shim"

    def log_message(self, fmt, *args):  # noqa: A003 - one line per model call is noise
        pass

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler naming
        self._proxy()

    def do_GET(self):  # noqa: N802
        self._proxy()

    def _fail(self, status: int, message: str) -> None:
        body = json.dumps(
            {"type": "error", "error": {"type": "shim_error", "message": message}}
        ).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _proxy(self) -> None:
        auth = self.headers.get("Authorization", "")
        local = auth[7:].strip() if auth[:7].lower() == "bearer " else ""
        grant = _grant_for(local)
        if grant is None:
            self._fail(401, "no active model grant for this token")
            return

        target = urllib.parse.urlsplit(str(grant["endpoint"]).rstrip("/") + self.path)
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""

        sigv4 = bool(grant.get("access_key_id"))
        passthrough = {
            k: v
            for k, v in self.headers.items()
            if k.lower() not in _DROP_REQUEST_HEADERS
            and not (sigv4 and k.lower().startswith(_SIGV4_RESERVED_PREFIX))
        }

        if sigv4:
            # Fail fast on a model this session was not routed to. This is an
            # optimisation, not an authorization boundary: the session's user
            # is root in this microVM and can bypass the shim. Real enforcement
            # has to sit outside the container — see the platform's
            # llm_credentials_service.mint_agentcore docstring.
            allowed = [str(m) for m in (grant.get("allowed_models") or [])]
            if allowed and body:
                try:
                    requested = str(json.loads(body).get("model") or "")
                except Exception:  # noqa: BLE001 - non-JSON bodies just pass
                    requested = ""
                if requested and requested not in allowed:
                    self._fail(
                        403, f"model {requested!r} is not permitted for this session"
                    )
                    return
            headers = _sign_sigv4(
                grant,
                self.command,
                str(grant["endpoint"]).rstrip("/") + self.path,
                body,
            )
            # Anything that took part in the signature must keep the signed
            # value: the caller's own content-type would otherwise silently
            # invalidate it.
            signed = {k.lower() for k in headers}
            headers.update(
                {k: v for k, v in passthrough.items() if k.lower() not in signed}
            )
        else:
            headers = passthrough
            headers["Authorization"] = f"Bearer {grant['token']}"
            headers["x-platform-session-id"] = str(grant.get("session_id") or "")

        headers["Accept-Encoding"] = "identity"
        headers["Content-Length"] = str(len(body))

        conn_cls = (
            http.client.HTTPSConnection
            if target.scheme == "https"
            else http.client.HTTPConnection
        )
        conn = conn_cls(target.netloc, timeout=15 * 60)
        path = target.path + (f"?{target.query}" if target.query else "")

        try:
            conn.request(self.command, path, body=body, headers=headers)
            resp = conn.getresponse()
        except Exception as e:  # noqa: BLE001 - report any transport failure as 502
            conn.close()
            logger.warning("llm-shim could not reach the edge: %s", e)
            self._fail(502, f"model edge unreachable: {e}")
            return

        try:
            self.send_response(resp.status)
            for k, v in resp.getheaders():
                if k.lower() not in _DROP_RESPONSE_HEADERS:
                    # http.server does not validate outgoing headers, so strip
                    # CR/LF before echoing the edge's headers back — a
                    # misbehaving upstream must not be able to smuggle extra
                    # header lines into our response.
                    self.send_header(
                        k.replace("\r", "").replace("\n", ""),
                        v.replace("\r", "").replace("\n", ""),
                    )
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            # read1 returns what has arrived rather than waiting for a full
            # buffer. Plain read(n) would hold each SSE frame until 8 KB had
            # accumulated, which is the difference between a token stream and a
            # response that appears all at once.
            while True:
                chunk = resp.read1(8192)
                if not chunk:
                    break
                self.wfile.write(b"%x\r\n" % len(chunk) + chunk + b"\r\n")
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except Exception as e:  # noqa: BLE001
            # Response already begun: the only honest thing left is to drop the
            # connection so the client sees a truncated stream rather than a
            # silently short answer.
            logger.warning("llm-shim stream aborted: %s", e)
            self.close_connection = True
        finally:
            conn.close()


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    # Concurrent agent runs share this shim; a stuck upstream must not block
    # the accept loop.
    allow_reuse_address = True


def start() -> None:
    """Start the shim once. Loopback only: nothing outside this container
    should be able to spend an invocation's model allowance."""
    global _started
    with _lock:
        if _started:
            return
        _started = True
    server = _Server(("127.0.0.1", PORT), _Handler)
    threading.Thread(target=server.serve_forever, name="llm-shim", daemon=True).start()
    logger.info("llm-shim listening on %s", BASE_URL)
