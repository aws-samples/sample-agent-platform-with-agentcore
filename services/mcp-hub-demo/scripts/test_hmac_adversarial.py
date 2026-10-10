"""Offline self-test of the MCPHUB-HMAC-SHA256 layer — no services needed.

Signs requests with the client-side class and checks that hub-side
verification accepts them, and that every kind of tampering is rejected.

Usage:  python scripts/test_hmac.py
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hub"))

from mcphub_hmac import (  # noqa: E402
    HmacVerificationError,
    McpHubHmacSignature,
    verify_request,
)

AK, SK = "demo-agent", "super-secret-value"
SSO = "eyJhbGciOi.fake-but-hashed.token"
URL = "http://hub.internal:8000/mcp"
BODY = b'{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'

passed = failed = 0


def check(name: str, fn, expect_ok: bool) -> None:
    global passed, failed
    try:
        fn()
        ok = expect_ok
        detail = "accepted"
    except HmacVerificationError as exc:
        ok = not expect_ok
        detail = f"rejected: {exc}"
    status = "PASS" if ok else "FAIL"
    print(f"  [{status}] {name} — {detail}")
    passed += ok
    failed += not ok


def signed(**overrides) -> dict[str, str]:
    kwargs = dict(
        method="POST", url=URL, raw_body=BODY, sso_token=SSO,
        content_type="application/json", access_key=AK, secret_key=SK,
    )
    kwargs.update(overrides)
    return McpHubHmacSignature.sign(**kwargs)


def verify(headers: dict[str, str], *, url: str = "/mcp", body: bytes = BODY,
           lookup=None, skew: int = 300):
    lower = {k.lower(): v for k, v in headers.items()}
    return verify_request(
        method="POST", url=url, raw_body=body, headers=lower,
        secret_key_lookup=lookup or {AK: SK}.get, max_clock_skew_s=skew,
    )


def main() -> int:
    print("MCPHUB-HMAC-SHA256 round-trip:")

    check("valid signature verifies", lambda: verify(signed()), expect_ok=True)

    check(
        "path with query string verifies",
        lambda: verify(
            signed(url=URL + "?b=2&a=1"), url="/mcp?b=2&a=1"
        ),
        expect_ok=True,
    )

    check(
        "client signed full URL, hub sees only the path",
        lambda: verify(signed(url="https://elsewhere.example.com/mcp")),
        expect_ok=True,
    )

    def tampered_body():
        verify(signed(), body=BODY.replace(b"tools/list", b"tools/call"))
    check("tampered body rejected", tampered_body, expect_ok=False)

    def swapped_token():
        headers = signed()
        headers["X-MCPHUB-SSO-TOKEN"] = "another.users.token"
        verify(headers)
    check("swapped SSO token rejected", swapped_token, expect_ok=False)

    def wrong_secret():
        verify(signed(), lookup={AK: "wrong"}.get)
    check("wrong secret rejected", wrong_secret, expect_ok=False)

    def unknown_actor():
        verify(signed(), lookup={}.get)
    check("unknown access key rejected", unknown_actor, expect_ok=False)

    def stale_timestamp():
        verify(signed(timestamp=int(time.time()) - 3600))
    check("timestamp outside window rejected", stale_timestamp, expect_ok=False)

    def replayed_to_other_path():
        verify(signed(), url="/other")
    check("signature replayed to a different path rejected",
          replayed_to_other_path, expect_ok=False)

    def missing_nonce():
        headers = signed()
        del headers["X-MCPHUB-CLIENT-NONCE"]
        verify(headers)
    check("missing nonce header rejected", missing_nonce, expect_ok=False)

    def bearer_scheme():
        verify({"Authorization": "Bearer whatever"})
    check("non-HMAC authorization rejected by this layer",
          bearer_scheme, expect_ok=False)

    # a downstream/proxy hop that collapses the path's trailing slash must not
    # break verification — both sides normalize through the same class
    check(
        "trailing-slash path normalization matches",
        lambda: verify(signed(url=URL + "/"), url="/mcp"),
        expect_ok=True,
    )

    print(f"\n{passed} passed, {failed} failed")

    assert failed == 0, f"{failed} HMAC checks failed"

    return 0


if __name__ == "__main__":
    sys.exit(main())
