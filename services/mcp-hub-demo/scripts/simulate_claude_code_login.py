"""Simulate Claude Code's exact OAuth flow against this stack, headlessly.

Claude Code authenticates to a remote MCP server like this:
  1. POST the MCP endpoint, get 401 with a `resource_metadata` pointer
  2. GET the Protected Resource Metadata -> find the authorization server
  3. GET the AS metadata -> see CIMD support (client_id_metadata_document_supported)
  4. Authorization request with client_id = https://claude.ai/oauth/claude-code-client-metadata
     (a URL! Keycloak fetches the client metadata from it — no registration),
     PKCE S256, redirect to a loopback port
  5. User signs in and approves the consent screen in the browser
     (here: scripted form POSTs)
  6. Exchange the code for tokens; call the MCP server with the access token

This script performs the same steps with a scripted login instead of a
browser, so the whole chain can be verified without human interaction. Pass a
username (default alice) to check what different departments end up seeing.

Usage: python scripts/simulate_claude_code_login.py [alice|bob|carol]
"""

import base64
import hashlib
import json
import os
import re
import secrets
import sys
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

import httpx2

HUB_MCP = os.environ.get("HUB_URL", "http://localhost:8000/mcp")
CLAUDE_CLIENT_ID = "https://claude.ai/oauth/claude-code-client-metadata"
REDIRECT_URI = "http://localhost:45678/callback"  # loopback, arbitrary port

PASSWORDS = {"alice": "alice-demo-password", "bob": "bob-demo-password", "carol": "carol-demo-password"}


class Browser:
    """Tiny scripted browser: manual cookie jar, manual redirects."""

    def __init__(self):
        self.cookies: dict[str, str] = {}
        self.http = httpx2.Client(follow_redirects=False, timeout=30.0)

    def request(self, method: str, url: str, **kw):
        resp = self.http.request(method, url, cookies=self.cookies, **kw)
        for name, value in resp.cookies.items():
            self.cookies[name] = value
        return resp

    def form_action(self, html: str, base_url: str) -> str:
        action = re.search(r'<form[^>]*action="([^"]+)"', html).group(1)
        return urljoin(base_url, action.replace("&amp;", "&"))


def main(username: str) -> int:
    password = PASSWORDS[username]
    browser = Browser()

    print("1. unauthenticated MCP request -> 401 + resource_metadata")
    r = httpx2.post(HUB_MCP, json={})
    assert r.status_code == 401, r.status_code
    prm_url = re.search(r'resource_metadata="([^"]+)"', r.headers["www-authenticate"]).group(1)
    print("   ", prm_url)

    print("2. protected resource metadata -> authorization server")
    prm = httpx2.get(prm_url).json()
    issuer = prm["authorization_servers"][0]
    assert prm["resource"] == HUB_MCP, prm
    print("   ", issuer)

    print("3. AS metadata -> CIMD supported?")
    meta = httpx2.get(f"{issuer}/.well-known/openid-configuration").json()
    assert meta.get("client_id_metadata_document_supported") is True, (
        "Keycloak must run with --features=cimd"
    )

    print("4. authorization request (client_id is a URL, PKCE S256)")
    verifier = secrets.token_urlsafe(48)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    auth_url = meta["authorization_endpoint"] + "?" + urlencode(
        {
            "response_type": "code",
            "client_id": CLAUDE_CLIENT_ID,
            "redirect_uri": REDIRECT_URI,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": secrets.token_urlsafe(16),
            "resource": HUB_MCP,  # RFC 8707; Keycloak ignores it (audience comes from the mapper)
        }
    )
    page = browser.request("GET", auth_url)
    assert page.status_code == 200, (page.status_code, page.text[:300])

    print("5. sign in, then approve the consent screen")
    resp = browser.request(
        "POST",
        browser.form_action(page.text, auth_url),
        data={"username": username, "password": password, "credentialId": ""},
    )
    # Follow the post-login redirects by hand until we land on the loopback
    # callback. CIMD clients get a consent screen on the way; approve it.
    for _ in range(6):
        if resp.status_code in (302, 303):
            location = resp.headers["location"]
            if location.startswith(REDIRECT_URI):
                break
            resp = browser.request("GET", location)
        elif resp.status_code == 200 and 'name="code"' in resp.text:
            hidden_code = re.search(r'name="code" value="([^"]+)"', resp.text).group(1)
            resp = browser.request(
                "POST",
                browser.form_action(resp.text, str(resp.request.url)),
                data={"code": hidden_code, "accept": "Yes"},
            )
            print("    consent screen approved")
        else:
            raise AssertionError(f"unexpected page: {resp.status_code} {resp.text[:300]}")
    else:
        raise AssertionError("never reached the loopback callback")
    code = parse_qs(urlparse(resp.headers["location"]).query)["code"][0]
    print("    got authorization code")

    print("6. exchange code for tokens (public client, no secret)")
    token = httpx2.post(
        meta["token_endpoint"],
        data={
            "grant_type": "authorization_code",
            "client_id": CLAUDE_CLIENT_ID,
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "code_verifier": verifier,
        },
    ).json()
    assert "access_token" in token, token
    access_token = token["access_token"]
    body = access_token.split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    print("    aud:", claims.get("aud"), "| department:", claims.get("department"),
          "| user:", claims.get("preferred_username"))
    print("    access token expires_in:", token.get("expires_in"),
          "| refresh token:", "yes" if token.get("refresh_token") else "no")
    assert claims.get("aud") == HUB_MCP, "audience mapper missing"
    assert claims.get("department"), "department claim missing"

    print("7. call the hub with the token")
    r = httpx2.post(
        HUB_MCP,
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2025-06-18",
        },
    )
    tools = [t["name"] for t in r.json()["result"]["tools"]]
    print("    tools for", username, "->", tools)

    print("\nOK — the full Claude Code OAuth chain works for", username)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "alice"))
