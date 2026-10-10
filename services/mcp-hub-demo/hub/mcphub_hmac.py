"""MCPHub inbound HMAC signing and verification. Single file, no third-party
dependencies.

Two halves:

- ``McpHubHmacSignature`` is the CLIENT-side signer, kept exactly as an
  application team would copy it: call ``sign()`` and send the returned
  headers together with the same ``raw_body``.
- ``parse_authorization`` / ``verify_request`` are the HUB-side counterparts:
  they rebuild the same canonical request from what actually arrived on the
  wire and compare signatures in constant time.

The scheme authenticates the *application* (the Actor named by the access
key). The end-user identity rides separately in ``X-MCPHUB-SSO-TOKEN`` — its
sha256 is part of the signed material, so the token cannot be swapped under
an existing signature, but validating the token itself (signature, issuer,
audience, expiry) is the hub's job, after the HMAC check passes.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import time
import uuid
from urllib.parse import quote_from_bytes, unquote, unquote_to_bytes, urlparse, urlsplit


class McpHubHmacSignature:
    ALGORITHM = "MCPHUB-HMAC-SHA256"
    HEADER_AUTHORIZATION = "Authorization"
    HEADER_SSO_TOKEN = "X-MCPHUB-SSO-TOKEN"
    HEADER_TIMESTAMP = "X-MCPHUB-CLIENT-TIMESTAMP"
    HEADER_NONCE = "X-MCPHUB-CLIENT-NONCE"
    HEADER_CONTENT_TYPE = "Content-Type"
    SIGNED_HEADERS = (
        "content-type",
        "path",
        "x-mcphub-client-nonce",
        "x-mcphub-client-timestamp",
        "x-mcphub-sso-token",
    )

    @staticmethod
    def normalize_url_path(url_or_path: str) -> str:
        text = (url_or_path or "").strip()
        if not text:
            raise ValueError("url path must not be empty")
        if "://" in text or text.startswith("//"):
            path = urlparse(text).path or "/"
        else:
            path = text.split("?", 1)[0] or "/"
        path = unquote(path)
        if not path.startswith("/"):
            path = "/" + path
        if path != "/":
            path = path.rstrip("/")
        return path

    @staticmethod
    def _normalize_header_value(value: str) -> str:
        if "\r" in value or "\n" in value:
            raise ValueError("header values must not contain CR or LF")
        return re.sub(r"\s+", " ", value.strip())

    @staticmethod
    def _encode_component(value: str) -> str:
        return quote_from_bytes(unquote_to_bytes(value), safe="-_.~")

    @classmethod
    def _canonical_query(cls, raw_query: str) -> str:
        if not raw_query:
            return ""
        pairs: list[tuple[str, str]] = []
        for part in raw_query.split("&"):
            name, separator, value = part.partition("=")
            if not separator:
                value = ""
            pairs.append((cls._encode_component(name), cls._encode_component(value)))
        pairs.sort()
        return "&".join(f"{name}={value}" for name, value in pairs)

    @classmethod
    def build_canonical_request(
        cls,
        *,
        method: str,
        url: str,
        raw_body: bytes,
        sso_token: str,
        content_type: str,
        timestamp: int,
        nonce: str,
    ) -> str:
        if not all((method, url, sso_token, content_type, nonce)):
            raise ValueError("signing fields must not be empty")
        parsed = urlsplit(url)
        path = cls.normalize_url_path(parsed.path)
        canonical_headers = {
            "content-type": cls._normalize_header_value(content_type),
            "path": path,
            "x-mcphub-client-nonce": cls._normalize_header_value(nonce),
            "x-mcphub-client-timestamp": str(int(timestamp)),
            "x-mcphub-sso-token": hashlib.sha256(sso_token.encode("utf-8")).hexdigest(),
        }
        header_block = "".join(
            f"{name}:{canonical_headers[name]}\n" for name in cls.SIGNED_HEADERS
        )
        return "\n".join(
            (
                method.upper(),
                path,
                cls._canonical_query(parsed.query),
                header_block,
                ";".join(cls.SIGNED_HEADERS),
                hashlib.sha256(raw_body).hexdigest(),
            )
        )

    @classmethod
    def sign(
        cls,
        *,
        method: str,
        url: str,
        raw_body: bytes,
        sso_token: str,
        content_type: str,
        access_key: str,
        secret_key: str,
        timestamp: int | None = None,
        nonce: str | None = None,
    ) -> dict[str, str]:
        if not access_key or not secret_key:
            raise ValueError("access_key and secret_key must not be empty")
        timestamp = int(time.time()) if timestamp is None else int(timestamp)
        nonce = str(uuid.uuid4()) if nonce is None else nonce
        canonical_request = cls.build_canonical_request(
            method=method,
            url=url,
            raw_body=raw_body,
            sso_token=sso_token,
            content_type=content_type,
            timestamp=timestamp,
            nonce=nonce,
        )
        string_to_sign = "\n".join(
            (
                cls.ALGORITHM,
                str(timestamp),
                hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
            )
        )
        signature = hmac.new(
            secret_key.encode("utf-8"),
            string_to_sign.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        signed_headers = ";".join(cls.SIGNED_HEADERS)
        return {
            cls.HEADER_CONTENT_TYPE: content_type,
            cls.HEADER_SSO_TOKEN: sso_token,
            cls.HEADER_TIMESTAMP: str(timestamp),
            cls.HEADER_NONCE: nonce,
            cls.HEADER_AUTHORIZATION: (
                f"{cls.ALGORITHM} Credential={access_key},"
                f"SignedHeaders={signed_headers},Signature={signature}"
            ),
        }


# ---------------------------------------------------------------------------
# Hub-side verification
# ---------------------------------------------------------------------------

# ±5 minutes, same tolerance SigV4 uses. A replayed request older than this is
# rejected on the timestamp alone; within the window, replay suppression would
# need a server-side nonce cache — deliberately out of scope for this sample
# (single hub instance, demo backends), flagged in the README as production
# hardening.
DEFAULT_CLOCK_SKEW_S = 300


class HmacVerificationError(ValueError):
    """Any reason the HMAC layer rejects a request. The message is safe to
    return to the caller (it never contains key material)."""


def parse_authorization(header: str) -> tuple[str, str, str]:
    """Split ``MCPHUB-HMAC-SHA256 Credential=…,SignedHeaders=…,Signature=…``
    into (access_key, signed_headers, signature)."""
    algorithm, _, rest = (header or "").strip().partition(" ")
    if algorithm != McpHubHmacSignature.ALGORITHM:
        raise HmacVerificationError("authorization scheme is not MCPHUB-HMAC-SHA256")
    fields: dict[str, str] = {}
    for part in rest.split(","):
        name, separator, value = part.strip().partition("=")
        if separator:
            fields[name] = value
    access_key = fields.get("Credential", "")
    signed_headers = fields.get("SignedHeaders", "")
    signature = fields.get("Signature", "")
    if not access_key or not signed_headers or not signature:
        raise HmacVerificationError(
            "authorization header must carry Credential, SignedHeaders and Signature"
        )
    return access_key, signed_headers, signature


def verify_request(
    *,
    method: str,
    url: str,
    raw_body: bytes,
    headers: dict[str, str],
    secret_key_lookup,
    max_clock_skew_s: int = DEFAULT_CLOCK_SKEW_S,
) -> tuple[str, str]:
    """Verify one inbound request. Returns ``(access_key, sso_token)``.

    ``headers`` is a lowercase-name → value mapping of the request headers as
    received; ``url`` is the request path plus query string (the canonical
    request only ever uses path and query, so scheme/host need not be known).
    ``secret_key_lookup(access_key)`` returns the shared secret or None for an
    unknown Actor.

    Raises :class:`HmacVerificationError` with a caller-safe message on any
    failure. The signature is checked with ``hmac.compare_digest``; the
    canonical request is rebuilt with the same class the client signs with, so
    the two sides cannot drift apart.
    """
    access_key, signed_headers, signature = parse_authorization(
        headers.get("authorization", "")
    )
    expected_signed = ";".join(McpHubHmacSignature.SIGNED_HEADERS)
    if signed_headers != expected_signed:
        raise HmacVerificationError(f"SignedHeaders must be {expected_signed}")

    timestamp_raw = headers.get("x-mcphub-client-timestamp", "")
    try:
        timestamp = int(timestamp_raw)
    except ValueError:
        raise HmacVerificationError("x-mcphub-client-timestamp must be an integer")
    if abs(int(time.time()) - timestamp) > max_clock_skew_s:
        raise HmacVerificationError(
            f"request timestamp outside the ±{max_clock_skew_s}s window"
        )

    secret_key = secret_key_lookup(access_key)
    if not secret_key:
        raise HmacVerificationError(f"unknown access key: {access_key}")

    sso_token = headers.get("x-mcphub-sso-token", "")
    try:
        canonical_request = McpHubHmacSignature.build_canonical_request(
            method=method,
            url=url,
            raw_body=raw_body,
            sso_token=sso_token,
            content_type=headers.get("content-type", ""),
            timestamp=timestamp,
            nonce=headers.get("x-mcphub-client-nonce", ""),
        )
    except ValueError as exc:
        raise HmacVerificationError(str(exc))

    string_to_sign = "\n".join(
        (
            McpHubHmacSignature.ALGORITHM,
            str(timestamp),
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        )
    )
    expected = hmac.new(
        secret_key.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise HmacVerificationError("signature mismatch")
    return access_key, sso_token
