#!/usr/bin/env python3
"""The headless kernel's model shim renewing a 15-minute AgentCore credential
(runtimes/agent-sdk-kernel/src/llm_shim.py).

An AgentCore gateway credential lives 15 minutes and the platform revokes a
session by refusing to renew it. The shim is what renews. The real shim runs
on loopback; one local HTTP server plays both the backend's renew route and
the gateway, recording which access key signed each gateway request.

Invariants:
    R1  a credential inside RENEW_BEFORE_S is renewed before the request is
        signed, and that request is signed with the new credential
    R2  a credential not yet due is not renewed (no call to the backend)
    R3  a 401 from the backend is final: the shim stops asking and keeps
        signing with the credential it has until that expires upstream
    R4  concurrent requests on one due grant cause one renewal, not one each
    R5  a renewal can move only the credential: endpoint, region, service and
        the model allowlist stay what the invocation was routed to
    R6  a transport failure is not final: the next request retries
    R7  concurrent requests that all reach a due grant whose renewal is then
        refused cause one backend call, not one each
    R8  a 429 (another client of the session renewed a moment ago) is not a
        refusal: the shim backs off for Retry-After, then renews
    R10 a 3xx from the renewal URL is never followed (the body carries the
        renewal token)
    R9  a 200 whose body is not a credential (a list, a string, null, wrong
        field types, non-JSON) never fails the model request: the old
        credential signs it; and no answer can move the renewal URL

Run: python3 runtimes/agent-sdk-kernel/tests/test_llm_shim_renewal_adversarial.py
"""

import http.server
import json
import sys
import threading
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import llm_shim  # noqa: E402

STATE = {"renew_calls": 0, "renew_status": 200, "renew_delay": 0.0, "signed_with": [],
         "renew_override": {}, "next_key": 1}
LOCK = threading.Lock()


def ok(msg):
    print(f"  ok  {msg}")


class Upstream(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):  # noqa: N802
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.path == "/steal":
            with LOCK:
                STATE["stolen"].append(body)
            self._send(200, {"llm_credentials": {}})
            return
        if self.path == "/renew" and STATE.get("redirect"):
            with LOCK:
                STATE["renew_calls"] += 1
            self.send_response(STATE["redirect"])
            self.send_header("Location", URL + "/steal")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path == "/renew":
            with LOCK:
                STATE["renew_calls"] += 1
                status, delay = STATE["renew_status"], STATE["renew_delay"]
                key = f"ASIARENEWED{STATE['next_key']}"
                STATE["next_key"] += 1
            time.sleep(delay)
            if status == 429:
                data = json.dumps({"detail": "retry"}).encode()
                self.send_response(429)
                self.send_header("Retry-After", str(STATE.get("retry_after", 1)))
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            if status != 200:
                self._send(status, {"detail": "Renewal refused"})
                return
            fresh = {"access_key_id": key, "secret_access_key": "s2", "session_token": "t2",
                     "expires_at": int(time.time()) + 900,
                     "renew": {"url": URL + "/renew", "token": "tok", "grant_expires_at": int(time.time()) + 3600}}
            fresh.update(STATE["renew_override"])
            if "raw_body" in STATE:
                data = STATE["raw_body"]
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            self._send(200, {"llm_credentials": fresh})
            return
        auth = self.headers.get("Authorization", "")
        key = auth.split("Credential=", 1)[1].split("/", 1)[0] if "Credential=" in auth else ""
        with LOCK:
            STATE["signed_with"].append((self.path, key))
        self._send(200, {"ok": True, "len": len(body)})

    def _send(self, status, obj):
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
URL = f"http://127.0.0.1:{server.server_address[1]}"
threading.Thread(target=server.serve_forever, daemon=True).start()


def grant(expires_in: int, renew_url: str | None = None) -> dict:
    return {
        "mode": "agentcore_gateway", "endpoint": URL + "/gw", "session_id": "sid-1",
        "region": "us-east-1", "service": "bedrock-agentcore",
        "access_key_id": "ASIAORIGINAL", "secret_access_key": "s1", "session_token": "t1",
        "expires_at": int(time.time()) + expires_in, "allowed_models": ["claude"],
        "renew": {"url": renew_url if renew_url is not None else URL + "/renew", "token": "tok",
                  "grant_expires_at": int(time.time()) + 3600},
    }


def call(local: str, model: str = "claude") -> int:
    req = urllib.request.Request(
        llm_shim.BASE_URL + "/v1/messages", method="POST",
        data=json.dumps({"model": model, "max_tokens": 1}).encode(),
        headers={"Authorization": f"Bearer {local}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def reset():
    with LOCK:
        STATE.update(renew_calls=0, renew_status=200, renew_delay=0.0, signed_with=[], renew_override={}, retry_after=1, stolen=[])
        STATE.pop("raw_body", None)


def main() -> int:
    llm_shim.start()
    time.sleep(0.2)

    # R1
    reset()
    g = grant(expires_in=120)
    tok = llm_shim.register(g)
    assert call(tok) == 200
    assert STATE["renew_calls"] == 1, STATE
    assert STATE["signed_with"][-1][1].startswith("ASIARENEWED"), STATE["signed_with"]
    assert g["expires_at"] - time.time() > 800
    llm_shim.release(tok)
    ok("R1 a due credential is renewed and the request is signed with the new one")

    # R2
    reset()
    g = grant(expires_in=800)
    tok = llm_shim.register(g)
    for _ in range(3):
        assert call(tok) == 200
    assert STATE["renew_calls"] == 0, STATE
    assert {k for _, k in STATE["signed_with"]} == {"ASIAORIGINAL"}
    llm_shim.release(tok)
    ok("R2 a credential not yet due is not renewed")

    # R3
    reset()
    STATE["renew_status"] = 401
    g = grant(expires_in=120)
    tok = llm_shim.register(g)
    for _ in range(4):
        assert call(tok) == 200
    assert STATE["renew_calls"] == 1, f"a refused renewal must not be retried: {STATE['renew_calls']}"
    assert {k for _, k in STATE["signed_with"]} == {"ASIAORIGINAL"}
    llm_shim.release(tok)
    ok("R3 a 401 is final: no more renewal attempts, the old credential runs to expiry")

    # R4
    reset()
    STATE["renew_delay"] = 0.5
    g = grant(expires_in=60)
    tok = llm_shim.register(g)
    results = []
    threads = [threading.Thread(target=lambda: results.append(call(tok))) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == [200] * 8, results
    assert STATE["renew_calls"] == 1, f"one renewal for concurrent requests, got {STATE['renew_calls']}"
    keys = {k for _, k in STATE["signed_with"]}
    assert len(keys) == 1 and keys.pop().startswith("ASIARENEWED"), STATE["signed_with"]
    llm_shim.release(tok)
    ok("R4 concurrent requests on a due grant cause exactly one renewal")

    # R5
    reset()
    STATE["renew_override"] = {"endpoint": "http://attacker.invalid", "region": "eu-west-1",
                               "service": "s3", "allowed_models": ["*"]}
    g = grant(expires_in=60)
    tok = llm_shim.register(g)
    assert call(tok) == 200
    assert g["endpoint"] == URL + "/gw" and g["region"] == "us-east-1"
    assert g["service"] == "bedrock-agentcore" and g["allowed_models"] == ["claude"]
    assert call(tok, model="other-model") == 403, "the allowlist did not widen"
    assert STATE["signed_with"][-1][0] == "/gw/v1/messages"
    llm_shim.release(tok)
    ok("R5 a renewal moves only the credential, never the routing or the allowlist")

    # R6
    reset()
    g = grant(expires_in=60, renew_url="http://127.0.0.1:9/renew")
    tok = llm_shim.register(g)
    assert call(tok) == 200
    assert not g["renew"].get("refused"), "a transport failure is not a refusal"
    g["renew"]["url"] = URL + "/renew"
    assert call(tok) == 200
    assert STATE["renew_calls"] == 1 and g["access_key_id"].startswith("ASIARENEWED")
    llm_shim.release(tok)
    ok("R6 a transport failure is retried on the next request")

    # R7
    reset()
    STATE["renew_status"] = 401
    STATE["renew_delay"] = 0.5
    g = grant(expires_in=60)
    tok = llm_shim.register(g)
    results = []
    threads = [threading.Thread(target=lambda: results.append(call(tok))) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == [200] * 8, results
    assert STATE["renew_calls"] == 1, f"a refusal must end renewal for every waiting request, got {STATE['renew_calls']} calls"
    llm_shim.release(tok)
    ok("R7 concurrent requests on a grant whose renewal is refused cause one backend call")

    # R8
    reset()
    STATE["renew_status"] = 429
    STATE["retry_after"] = 1
    g = grant(expires_in=60)
    tok = llm_shim.register(g)
    for _ in range(3):
        assert call(tok) == 200
    assert STATE["renew_calls"] == 1, f"inside Retry-After the shim must not ask again: {STATE['renew_calls']}"
    assert not g["renew"].get("refused"), "a throttle is not a refusal"
    STATE["renew_status"] = 200
    time.sleep(1.1)
    assert call(tok) == 200
    assert STATE["renew_calls"] == 2 and g["access_key_id"].startswith("ASIARENEWED"), STATE
    llm_shim.release(tok)
    ok("R8 a 429 backs off for Retry-After and is then renewed, never treated as a refusal")

    # R9
    hostile = [b'{"llm_credentials":[1]}', b'{"llm_credentials":"x"}', b'{"llm_credentials":null}',
               b'[1,2]', b'"str"', b'null', b'not json',
               b'{"llm_credentials":{"access_key_id":1,"secret_access_key":"s","session_token":"t","expires_at":1}}',
               b'{"llm_credentials":{"access_key_id":"A","secret_access_key":"s","session_token":"t","expires_at":"soon"}}',
               b'{"llm_credentials":{"access_key_id":"A","secret_access_key":"s","session_token":"t","expires_at":true}}',
               b'{"llm_credentials":{"access_key_id":"A"}}']
    now = int(time.time())
    for exp in ("1e309", "-1e309", "NaN", "Infinity", str(now - 10), str(now + 10 * 3600), "0"):
        hostile.append(('{"llm_credentials":{"access_key_id":"A","secret_access_key":"s","session_token":"t","expires_at":%s}}' % exp).encode())
    for body in hostile:
        reset()
        STATE["raw_body"] = body
        g = grant(expires_in=60)
        tok = llm_shim.register(g)
        assert call(tok) == 200, f"the model request failed on renewal answer {body!r}"
        assert STATE["signed_with"][-1][1] == "ASIAORIGINAL" and g["access_key_id"] == "ASIAORIGINAL", body
        # and the grant still renews afterwards: nothing in the answer stuck
        STATE.pop("raw_body", None)
        g["renew"].pop("retry_at", None)
        assert call(tok) == 200 and g["access_key_id"].startswith("ASIARENEWED"), f"grant wedged after {body!r}"
        llm_shim.release(tok)
    reset()
    STATE["renew_override"] = {"renew": {"url": "http://attacker.invalid/renew", "token": "tok2", "grant_expires_at": 1}}
    g = grant(expires_in=60)
    tok = llm_shim.register(g)
    assert call(tok) == 200
    assert g["access_key_id"].startswith("ASIARENEWED") and g["renew"]["url"] == URL + "/renew", g["renew"]
    assert g["renew"]["token"] == "tok2"
    llm_shim.release(tok)
    for bad in ("", 7, None, ["t"]):
        reset()
        STATE["renew_override"] = {"renew": {"url": URL + "/renew", "token": bad, "grant_expires_at": 1e309}}
        g = grant(expires_in=60)
        tok = llm_shim.register(g)
        assert call(tok) == 200 and g["renew"]["token"] == "tok", f"renew token replaced by {bad!r}"
        g["expires_at"] = int(time.time()) + 60
        STATE["renew_override"] = {}
        assert call(tok) == 200 and STATE["renew_calls"] == 2, f"renewal ended after a renew.token of {bad!r}"
        llm_shim.release(tok)
    ok("R9 a non-credential answer never fails the request and no answer moves the renewal URL")

    # R10
    for code in (301, 302, 303, 307, 308):
        reset()
        STATE["redirect"] = code
        g = grant(expires_in=60)
        tok = llm_shim.register(g)
        assert call(tok) == 200, code
        assert STATE["stolen"] == [], f"a {code} redirect resent the renewal body: {STATE['stolen']}"
        assert g["access_key_id"] == "ASIAORIGINAL"
        llm_shim.release(tok)
    STATE.pop("redirect", None)
    ok("R10 a redirect is never followed, so the renewal token goes nowhere else")

    print("llm shim renewal: all assertions passed")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as e:
        print(f"  FAIL {e}")
        sys.exit(1)
