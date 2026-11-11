"""Adversarial tests for images sent with a prompt
(app/services/images.py, invocation_service, kernel_service).

An image is caller input that reaches the model and the microVM, so these
tests try to send what the platform does not accept, make a refused image
cost quota, and make the bytes land where they should not.

Invariants:
    I1  only at most four base64 png, jpeg, gif or webp images whose bytes
        match their declared type, each at most 3.75 MB, are accepted; a URL
        source, a missing or non-base64 payload, a mismatched type or an
        oversized image is a ValueError
    I2  validation happens before governance on both paths: a refused image
        neither counts against the quota nor reaches the kernel
    I3  valid images reach the kernel payload as {media_type, data}; a
        prompt without images sends no images field
    I4  the event trail records each image's type, size and digest, never
        its bytes
"""

from __future__ import annotations

import base64
import hashlib
import json
import os

os.environ.setdefault("PLATFORM_AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")

import pytest

from app.services import images as image_rules
from app.services import invocation_service as inv
from app.services import kernel_service as ks

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x01" * 64
GIF = b"GIF89a" + b"\x02" * 16
WEBP = b"RIFF\x00\x00\x00\x00WEBP" + b"\x03" * 16


def img(raw: bytes, media_type: str | None = None) -> dict:
    return {"media_type": media_type or image_rules.sniff(raw), "data": base64.b64encode(raw).decode()}


# I1 ---------------------------------------------------------------------
def test_i1_accepts_only_bounded_typed_base64_images():
    ok = image_rules.validate([img(PNG), img(JPEG), img(GIF), img(WEBP)])
    assert [i["media_type"] for i in ok] == ["image/png", "image/jpeg", "image/gif", "image/webp"]
    assert image_rules.validate(None) == [] and image_rules.validate([]) == []


@pytest.mark.parametrize("images", [
    [img(PNG)] * 5,                                                     # too many
    [img(PNG, "image/jpeg")],                                           # declared type is not the content's
    [img(b"<svg xmlns='http://www.w3.org/2000/svg'/>", "image/svg+xml")],
    [img(b"RIFF\x00\x00\x00\x00WAVE", "image/webp")],                    # RIFF but not WebP
    [{"media_type": "image/png", "data": "not base64!"}],
    [{"media_type": "image/png"}],
    [{"media_type": "image/png", "source": {"type": "url", "url": "http://169.254.169.254/latest/meta-data/"}}],
    [img(PNG + b"\x00" * image_rules.MAX_IMAGE_BYTES)],                 # over the size limit
    "not a list",
    [b"raw bytes"],
])
def test_i1_refuses_everything_else(images):
    with pytest.raises(ValueError):
        image_rules.validate(images)


def test_i1_only_base64_data_is_forwarded():
    out = image_rules.validate([dict(img(PNG), source={"type": "url", "url": "http://example.invalid/x.png"})])
    assert out == [img(PNG)], "extra fields (a URL source) are dropped, never forwarded"


# I2 ---------------------------------------------------------------------
@pytest.fixture
def counted(monkeypatch):
    calls = {"governance": 0, "kernel": []}

    def check(user, source, mt=None):
        calls["governance"] += 1
        return mt or 10
    monkeypatch.setattr(inv.governance_service, "check_and_count", check)
    monkeypatch.setattr(inv.observability_service, "record", lambda **kw: None)
    monkeypatch.setattr(inv.model_config_service, "resolve", lambda b, m: None)
    monkeypatch.setattr(inv.kernel_service, "invoke_sdk_kernel",
                        lambda **kw: calls["kernel"].append(kw) or {"ok": True, "result": "r", "usage": {}, "raw": {}})

    def stream(**kw):
        calls["kernel"].append(kw)
        yield {"type": "result", "ok": True, "result": "r", "usage": {}}
    monkeypatch.setattr(inv.kernel_service, "invoke_sdk_kernel_stream", stream)
    return calls


def test_i2_a_refused_image_costs_no_quota_and_no_kernel_call(counted):
    bad = [img(PNG, "image/gif")]
    with pytest.raises(ValueError):
        inv.invoke(user="u", source="channel", prompt="look", images=bad)
    with pytest.raises(ValueError):
        inv.start_stream(user="u", source="channel", prompt="look", images=bad)
    assert counted["governance"] == 0 and counted["kernel"] == []


# I3 ---------------------------------------------------------------------
def test_i3_images_reach_the_kernel(counted):
    inv.invoke(user="u", source="channel", prompt="look", images=[img(PNG)])
    list(inv.start_stream(user="u", source="channel", prompt="look", images=[img(JPEG)]))
    assert [k["images"] for k in counted["kernel"]] == [[img(PNG)], [img(JPEG)]]


def test_i3_the_payload_carries_images_only_when_there_are_some(monkeypatch):
    s = ks.KernelService.__new__(ks.KernelService)
    monkeypatch.setattr(ks, "runtime_arn", lambda kind, pv: "arn:aws:bedrock-agentcore:us-east-1:000:runtime/k")
    with_images = s._prepare("p", None, 3, "s-1", None, None, None, None, None, "u", None, "", "", False, [img(PNG)])
    without = s._prepare("p", None, 3, "s-1", None, None, None, None, None, "u", None, "", "", False)
    assert with_images["payload"]["images"] == [img(PNG)] and "images" not in without["payload"]


# I4 ---------------------------------------------------------------------
def test_i4_the_trail_records_digests_never_bytes(counted, monkeypatch):
    puts = []
    monkeypatch.setattr(inv.boto3, "client", lambda *a, **k: type("S3", (), {"put_object": lambda self, **kw: puts.append(kw)})())
    monkeypatch.setattr(inv.settings, "workspace_bucket", "wb")
    inv.invoke(user="u", source="channel", prompt="look", images=[img(PNG), img(JPEG)])
    body = puts[0]["Body"].decode()
    header = json.loads(body.splitlines()[0])
    assert header["images"] == [{"media_type": "image/png", "bytes": len(PNG), "sha256": hashlib.sha256(PNG).hexdigest()},
                                {"media_type": "image/jpeg", "bytes": len(JPEG), "sha256": hashlib.sha256(JPEG).hexdigest()}]
    assert base64.b64encode(PNG).decode() not in body and base64.b64encode(JPEG).decode() not in body
