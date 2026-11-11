"""Images sent with a prompt (a Lark image message, an API call).

The kernel accepts at most four base64 images of a few types and sizes
(runtimes/agent-sdk-kernel/src/main.py, prompt_with_images) and checks them
again; the backend checks first, so a refused image costs no kernel call and
never reaches the microVM. Only base64 data is accepted: a URL source would
let the caller choose what the model (and the runtime's network) fetches.
"""

from __future__ import annotations

import base64
import binascii
import hashlib

MAX_IMAGES = 4
MAX_IMAGE_BYTES = 3_750_000  # Bedrock's limit for one image
_SIGNATURES = {
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/gif": (b"GIF87a", b"GIF89a"),
    "image/webp": (b"RIFF",),
}


def sniff(raw: bytes) -> str:
    """The media type of image bytes, by their signature; "" when not one of
    the accepted types."""
    for media_type, signatures in _SIGNATURES.items():
        if raw.startswith(signatures) and (media_type != "image/webp" or raw[8:12] == b"WEBP"):
            return media_type
    return ""


def from_bytes(raw: bytes) -> dict:
    """One image for the kernel payload from raw bytes; raises ValueError."""
    media_type = sniff(raw)
    if not media_type:
        raise ValueError("images must be png, jpeg, gif or webp")
    if len(raw) > MAX_IMAGE_BYTES:
        raise ValueError(f"an image must be at most {MAX_IMAGE_BYTES} bytes")
    return {"media_type": media_type, "data": base64.b64encode(raw).decode("ascii")}


def validate(images) -> list[dict]:
    """The images to send, as {media_type, data}; raises ValueError."""
    if not images:
        return []
    if not isinstance(images, list) or len(images) > MAX_IMAGES:
        raise ValueError(f"at most {MAX_IMAGES} images")
    out = []
    for im in images:
        if not isinstance(im, dict) or not isinstance(im.get("data"), str):
            raise ValueError("an image must be {media_type, data} with base64 data")
        try:
            raw = base64.b64decode(im["data"], validate=True)
        except (binascii.Error, ValueError):
            raise ValueError("image data is not base64") from None
        image = from_bytes(raw)
        if im.get("media_type") != image["media_type"]:
            raise ValueError("image content does not match its media type")
        out.append(image)
    return out


def describe(images: list[dict]) -> list[dict]:
    """What the event trail records about the images: type, size and digest,
    never the bytes."""
    out = []
    for im in images:
        raw = base64.b64decode(im["data"])
        out.append({"media_type": im["media_type"], "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()})
    return out
