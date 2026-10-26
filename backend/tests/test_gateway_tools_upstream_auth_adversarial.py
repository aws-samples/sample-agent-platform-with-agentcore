"""The Gateway page must not sign the user out when a gateway refuses their token.

GET /api/v1/gateways/{id}/tools lists a gateway's catalog with the caller's own
bearer token. An IAM-authorized gateway (the platform's inference gateway, the
sample-tools gateway) never accepts a bearer JWT and answers 401. The route used
to pass that status through, and the SPA treats any 401 as "the portal session
ended" (services/api.ts clears the session and redirects to /login): opening the
Gateway page, which auto-selects the first gateway, logged the user out.

The gateway's refusal is an upstream failure, not a portal-auth failure, so the
route must answer 502, and must keep passing other upstream statuses through.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.api import gateways as gw


def _patch(monkeypatch, status, resp):
    monkeypatch.setattr(gw, "_control", lambda: object())
    monkeypatch.setattr(
        gw, "_describe", lambda control, gid: {"mcp_url": "https://g.example/mcp", "targets": []}
    )
    monkeypatch.setattr(gw, "_mcp", lambda url, token, method, params=None: (status, resp))


@pytest.mark.parametrize("upstream", [401, 403])
def test_gateway_refusing_the_token_is_not_a_portal_401(monkeypatch, upstream):
    _patch(monkeypatch, upstream, {"error": "unauthorized"})
    with pytest.raises(HTTPException) as e:
        gw.list_gateway_tools("gw-1", user="admin", authorization="Bearer t")
    assert e.value.status_code == 502, "a gateway's 401/403 must not reach the SPA as 401"
    assert str(upstream) in str(e.value.detail)


def test_other_upstream_errors_keep_their_status(monkeypatch):
    _patch(monkeypatch, 429, {"error": "throttled"})
    with pytest.raises(HTTPException) as e:
        gw.list_gateway_tools("gw-1", user="admin", authorization="Bearer t")
    assert e.value.status_code == 429


def test_missing_bearer_is_still_a_401(monkeypatch):
    # the caller sent no token at all: that one *is* the portal's own auth
    _patch(monkeypatch, 200, {"result": {"tools": []}})
    with pytest.raises(HTTPException) as e:
        gw.list_gateway_tools("gw-1", user="admin", authorization="")
    assert e.value.status_code == 401


def test_success_lists_tools(monkeypatch):
    _patch(monkeypatch, 200, {"result": {"tools": [{"name": "t___x", "description": "d"}]}})
    out = gw.list_gateway_tools("gw-1", user="admin", authorization="Bearer t")
    assert out["gateway_id"] == "gw-1" and len(out["tools"]) == 1
