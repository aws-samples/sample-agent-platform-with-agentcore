"""An IAM-authorized gateway is reported as "not listable with your token", not as down.

GET /api/v1/gateways/{id}/tools lists a gateway's catalog with the caller's own
bearer token. An AWS_IAM gateway (inference, sample-tools, websearch) takes
SigV4 only, so that call is refused every time: the Gateway page showed every
such gateway red "not reachable" while agents, signing with their execution
role, used it fine. In an account whose gateways are all IAM (533 after the
migration) the whole page was red.

  1. an AWS_IAM gateway answers 200 with listable=False and a reason, and the
     caller's token is never sent to it (a refused bearer is noise in the
     gateway's auth logs and proves nothing)
  2. a JWT gateway is still listed live with the caller's token (the page's
     whole point: the catalog *this* identity would see), listable=True
  3. only the exact AWS_IAM type short-circuits: CUSTOM_JWT, an empty type and
     a lookalike ("aws_iam", "AWS_IAM " ) all go to the gateway
  4. a missing bearer is still the portal's own 401, IAM gateway or not
  5. a JWT gateway refusing the token still surfaces as 502 (the !63 rule)
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.api import gateways as gw


def _patch(monkeypatch, authorizer_type, status=200, resp=None):
    calls = []
    monkeypatch.setattr(gw, "_control", lambda: object())
    monkeypatch.setattr(
        gw,
        "_describe",
        lambda control, gid: {
            "mcp_url": "https://g.example/mcp",
            "targets": [{"name": "t", "enforcement": "gateway"}],
            "authorizer_type": authorizer_type,
        },
    )

    def fake_mcp(url, token, method, params=None):
        calls.append(token)
        return status, resp if resp is not None else {"result": {"tools": [{"name": "t___x"}]}}

    monkeypatch.setattr(gw, "_mcp", fake_mcp)
    return calls


# 1
def test_iam_gateway_is_not_listable_and_never_sees_the_token(monkeypatch):
    calls = _patch(monkeypatch, "AWS_IAM", status=401, resp={"error": "Invalid credentials"})
    out = gw.list_gateway_tools("gw-iam", user="admin", authorization="Bearer user-jwt")
    assert out["listable"] is False and out["tools"] == []
    assert "SigV4" in out["reason"]
    assert calls == [], "the user's bearer must not be sent to an IAM gateway"


# 2
def test_jwt_gateway_is_listed_live_with_the_callers_token(monkeypatch):
    calls = _patch(monkeypatch, "CUSTOM_JWT")
    out = gw.list_gateway_tools("gw-jwt", user="admin", authorization="Bearer user-jwt")
    assert out["listable"] is True and [t["name"] for t in out["tools"]] == ["t___x"]
    assert calls == ["user-jwt"]


# 3
@pytest.mark.parametrize("kind", ["CUSTOM_JWT", "", None, "aws_iam", "AWS_IAM ", "NONE"])
def test_only_exact_aws_iam_short_circuits(monkeypatch, kind):
    calls = _patch(monkeypatch, kind)
    out = gw.list_gateway_tools("gw-x", user="admin", authorization="Bearer user-jwt")
    assert out["listable"] is True and calls, f"{kind!r} must be listed against the gateway"


# 4
def test_missing_bearer_is_still_a_portal_401_on_an_iam_gateway(monkeypatch):
    _patch(monkeypatch, "AWS_IAM")
    with pytest.raises(HTTPException) as e:
        gw.list_gateway_tools("gw-iam", user="admin", authorization="")
    assert e.value.status_code == 401


# 5
def test_jwt_gateway_refusal_is_still_502(monkeypatch):
    _patch(monkeypatch, "CUSTOM_JWT", status=401, resp={"error": "bad token"})
    with pytest.raises(HTTPException) as e:
        gw.list_gateway_tools("gw-jwt", user="admin", authorization="Bearer user-jwt")
    assert e.value.status_code == 502
