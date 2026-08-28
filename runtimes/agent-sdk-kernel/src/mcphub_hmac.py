"""Client-side MCPHUB-HMAC-SHA256 signer. Single file, no third-party
dependencies.

This is the application-team signing class for an MCP hub that authenticates
calling *applications* with an access/secret key pair while the acting user's
SSO token rides separately in ``X-MCPHUB-SSO-TOKEN`` (its sha256 is part of
the signed material, so the token cannot be swapped under an existing
signature). Kept verbatim so the kernel signs byte-for-byte what the hub
verifies; the hub-side counterpart lives with the hub.

Usage: send the returned headers together with the same ``raw_body``.
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
