"""Adversarial tests for images sent to the lark-teammate in Lark
(app/integrations/lark/events.py parse, ingress.fetch_images and answer,
client.message_image).

An image in a group message is caller input that reaches the model. These
tests try to make the ingress fetch what it should not, pass an image the
platform does not accept, or download without bound.

Invariants:
    I1  image and rich-text messages yield their image keys (at most four);
        an image without text is answered with a neutral prompt; other
        message types stay unsupported
    I2  the ingress downloads only the current message's images, through
        ids checked by full match (a key with a path, a query or a trailing
        newline is refused), each bounded to the image size limit
    I3  only images the platform accepts (by their bytes) reach the
        pipeline; an unreadable one is left out and the agent is told, and a
        message that was only an unreadable image is answered without an
        agent call
"""

from __future__ import annotations

import io
import json
import os

os.environ.setdefault("PLATFORM_AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")

import pytest

from app.integrations.lark import events as ev
from app.integrations.lark import ingress
from app.integrations.lark.client import LarkClient
from app.services import images as image_rules

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
SVG = b"<svg xmlns='http://www.w3.org/2000/svg'/>"


def event(message_type, content, chat_type="p2p", message_id="om_1"):
    return {"sender": {"sender_id": {"open_id": "ou_alice"}},
            "message": {"chat_id": "oc_1", "message_id": message_id, "chat_type": chat_type,
                        "message_type": message_type, "content": json.dumps(content), "mentions": []}}


# I1 ---------------------------------------------------------------------
def test_i1_image_keys_from_image_and_rich_text_messages():
    m = ev.parse(event("image", {"image_key": "img_v3_a-1"}))
    assert m.supported and m.image_keys == ("img_v3_a-1",) and m.text == ev.IMAGE_ONLY
    post = {"content": [[{"tag": "text", "text": "what is this"}, {"tag": "img", "image_key": "img_1"}],
                        [{"tag": "img", "image_key": f"img_{i}"} for i in range(2, 9)]]}
    m = ev.parse(event("post", post))
    assert m.text == "what is this" and m.image_keys == ("img_1", "img_2", "img_3", "img_4")
    for t in ("file", "audio", "media", "sticker"):
        assert ev.parse(event(t, {"file_key": "f"})).supported is False


# I2 ---------------------------------------------------------------------
class Opener:
    def __init__(self, body=PNG):
        self.urls, self.body = [], body

    def __call__(self, req, timeout=None):
        self.urls.append(req.full_url)
        if req.full_url.endswith("/tenant_access_token/internal"):
            payload = json.dumps({"code": 0, "tenant_access_token": "t", "expire": 7200}).encode()
        else:
            payload = self.body

        class R(io.BytesIO):
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        return R(payload)


def test_i2_downloads_only_checked_ids_and_bounded():
    op = Opener()
    c = LarkClient(app_id="a", app_secret="s", base_url="https://open.feishu.cn", opener=op)
    assert c.message_image("om_1", "img_v3_a-1", 1000) == PNG
    assert op.urls[-1] == "https://open.feishu.cn/open-apis/im/v1/messages/om_1/resources/img_v3_a-1?type=image"
    for mid, key in (("om_1", "img_../../x"), ("om_1", "img_a?type=file"), ("om_1", "img_a\n"), ("om_1\n", "img_a"),
                     ("om_1", "file_abc"), ("../om", "img_a")):
        with pytest.raises(ValueError):
            c.message_image(mid, key, 1000)
    big = LarkClient(app_id="a", app_secret="s", base_url="https://open.feishu.cn", opener=Opener(PNG * 100))
    with pytest.raises(ValueError):
        big.message_image("om_1", "img_a", 100)


# I3 ---------------------------------------------------------------------
class Client:
    def __init__(self, images):
        self.images, self.asked, self.replies = images, [], []

    def message_image(self, message_id, key, limit):
        self.asked.append((message_id, key, limit))
        item = self.images[key]
        if isinstance(item, Exception):
            raise item
        return item

    def reply_text(self, mid, text):
        self.replies.append(text)

    def reply_card(self, *a):
        return "om_card"

    def card_id(self, *a):
        return "card"

    def update_card(self, *a):
        pass

    def recent_messages(self, *a):
        return []


def run(client, evt):
    calls = []

    def start_stream(**kw):
        calls.append(kw)
        yield {"type": "result", "ok": True, "result": "ok"}
    shown = ingress.answer(evt, client=client, start_stream=start_stream, shared_agent_id="s", history=0)
    return shown, calls


def test_i3_only_accepted_images_reach_the_pipeline():
    c = Client({"img_ok": PNG, "img_svg": SVG, "img_gone": RuntimeError("99991672 no im:resource")})
    post = {"content": [[{"tag": "text", "text": "compare"}, {"tag": "img", "image_key": "img_ok"},
                         {"tag": "img", "image_key": "img_svg"}, {"tag": "img", "image_key": "img_gone"}]]}
    shown, calls = run(c, event("post", post))
    (kw,) = calls
    assert kw["images"] == [image_rules.from_bytes(PNG)]
    assert "2 of the images could not be read" in kw["prompt"]
    assert all(a[0] == "om_1" and a[2] == image_rules.MAX_IMAGE_BYTES for a in c.asked)


def test_i3_an_unreadable_image_alone_is_answered_without_the_agent():
    c = Client({"img_svg": SVG})
    shown, calls = run(c, event("image", {"image_key": "img_svg"}))
    assert calls == [] and shown == ev.IMAGE_UNREADABLE and c.replies == [ev.IMAGE_UNREADABLE]


def test_i3_a_text_message_sends_no_images():
    shown, calls = run(Client({}), event("text", {"text": "hello"}))
    assert calls[0]["images"] is None


def test_i2_a_stale_token_is_refreshed_once():
    import urllib.error

    class Expiring(Opener):
        def __init__(self):
            super().__init__()
            self.tokens = 0

        def __call__(self, req, timeout=None):
            if req.full_url.endswith("/tenant_access_token/internal"):
                self.tokens += 1
            elif req.get_header("Authorization") == "Bearer t" and self.tokens == 1:
                raise urllib.error.HTTPError(req.full_url, 401, "expired", {}, None)
            return super().__call__(req, timeout)
    op = Expiring()
    c = LarkClient(app_id="a", app_secret="s", base_url="https://open.feishu.cn", opener=op)
    assert c.message_image("om_1", "img_a", 1000) == PNG and op.tokens == 2
