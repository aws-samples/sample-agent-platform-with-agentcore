"""Lark event security: request signature and payload decryption.

Lark signs every event callback with
``X-Lark-Signature = sha256(timestamp + nonce + encrypt_key + body)`` and,
when an Encrypt Key is configured, encrypts the body with AES-256-CBC
(key = sha256(encrypt_key), IV = the first 16 bytes of the ciphertext).
Both are Lark's specification; the cipher mode is not ours to choose.
Integrity comes from the signature, checked before anything is decrypted or
acted on, and from TLS. The one request Lark does not sign is the console's
URL verification; ingress.accept refuses every unsigned request before
decryption unless the operator's LARK_URL_CHALLENGE switch is on.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

# a request older (or newer) than this is a replay, not a delivery
MAX_SKEW_S = 300


def verify_signature(*, encrypt_key: str, timestamp: str, nonce: str, body: str,
                     signature: str, now: float | None = None) -> bool:
    if not (encrypt_key and timestamp and signature):
        return False
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False
    if abs((time.time() if now is None else now) - ts) > MAX_SKEW_S:
        return False
    expected = hashlib.sha256(f"{timestamp}{nonce}{encrypt_key}{body}".encode("utf-8")).hexdigest()
    return hmac.compare_digest(expected, signature)


def decrypt_event(*, encrypt_key: str, data: str) -> str:
    key = hashlib.sha256(encrypt_key.encode("utf-8")).digest()
    raw = base64.b64decode(data)
    if len(raw) < 32 or len(raw) % 16:
        raise ValueError("malformed encrypted event")
    # AES-256-CBC is mandated by Lark's event encryption; the signature above
    # authenticates the request.
    # nosemgrep: crypto-mode-without-authentication
    decryptor = Cipher(algorithms.AES(key), modes.CBC(raw[:16])).decryptor()
    padded = decryptor.update(raw[16:]) + decryptor.finalize()
    unpadder = PKCS7(128).unpadder()
    return (unpadder.update(padded) + unpadder.finalize()).decode("utf-8")
