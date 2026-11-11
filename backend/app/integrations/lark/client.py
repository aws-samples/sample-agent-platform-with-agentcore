"""The Lark Open API calls the ingress makes, as the app (tenant token).

Only what a turn needs: the images of a message, reply with a card (or post one to the chat), resolve the card's id, update it
in place, plain-text fallbacks, the current chat's recent messages, and a
Docx document shared with the chat. Standard library HTTP; the token is cached
for its lifetime minus a margin and refreshed once on a 401.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

_ID = re.compile(r"[A-Za-z0-9_]{1,128}")
_FILE_KEY = re.compile(r"file_[A-Za-z0-9_-]{1,124}")
_IMAGE_KEY = re.compile(r"img_[A-Za-z0-9_-]{1,124}")
TIMEOUT_S = 10


def _check_id(value: str, name: str) -> str:
    pattern = _FILE_KEY if name == "file_key" else _IMAGE_KEY if name == "image_key" else _ID
    # fullmatch: "$" would let a trailing newline through
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValueError(f"invalid Lark {name}")
    return value


def card_json(content: str, title: str | None = None) -> str:
    """Card JSON 2.0 with one markdown element; update_multi lets CardKit
    update it in place."""
    card: dict = {
        "schema": "2.0",
        "config": {"update_multi": True},
        "body": {"elements": [{"tag": "markdown", "content": content}]},
    }
    if title:
        card["header"] = {"title": {"tag": "plain_text", "content": title}}
    return json.dumps(card, ensure_ascii=False)


class LarkClient:
    def __init__(self, *, app_id: str, app_secret: str, base_url: str, opener=None) -> None:
        if not (app_id and app_secret):
            raise ValueError("Lark app id and secret are required")
        if not base_url.startswith("https://"):
            raise ValueError("Lark base URL must be https")
        self._app_id, self._app_secret = app_id, app_secret
        self._api = base_url.rstrip("/") + "/open-apis"
        self._open = opener or urllib.request.urlopen
        self._token = ""
        self._token_exp = 0.0

    # ----------------------------------------------------------- transport

    def _http(self, method: str, path: str, body: dict | None, token: str | None) -> tuple[int, dict]:
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self._api + path, data=data, method=method)
        req.add_header("Content-Type", "application/json; charset=utf-8")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with self._open(req, timeout=TIMEOUT_S) as resp:  # nosec B310 - https only, fixed host
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read() or b"{}")
            except ValueError:
                return e.code, {}

    def _download(self, path: str, limit: int) -> bytes:
        """GET a binary resource with the tenant token, refreshed once on a
        401 like every other call; at most ``limit`` bytes."""
        for attempt in (0, 1):
            req = urllib.request.Request(self._api + path, method="GET")
            req.add_header("Authorization", f"Bearer {self.tenant_token()}")
            try:
                with self._open(req, timeout=TIMEOUT_S) as resp:  # nosec B310 - https only, fixed host
                    data = resp.read(limit + 1)
                break
            except urllib.error.HTTPError as e:
                if e.code != 401 or attempt:
                    raise
                self._token = ""
        if len(data) > limit:
            raise ValueError("Lark resource exceeds the size limit")
        return data

    def tenant_token(self) -> str:
        if self._token and time.time() < self._token_exp:
            return self._token
        status, data = self._http("POST", "/auth/v3/tenant_access_token/internal",
                                  {"app_id": self._app_id, "app_secret": self._app_secret}, None)
        if status != 200 or data.get("code") != 0 or not data.get("tenant_access_token"):
            raise RuntimeError(f"Lark tenant token refused (http {status}, code {data.get('code')})")
        self._token = data["tenant_access_token"]
        self._token_exp = time.time() + int(data.get("expire", 7200)) - 300
        return self._token

    def _call_raw(self, method: str, path: str, body: dict | None = None) -> dict:
        """Like _call, returning the whole response (some endpoints, such as
        the bot info, answer outside ``data``)."""
        status, data = self._http(method, path, body, self.tenant_token())
        if status == 401:
            self._token = ""
            status, data = self._http(method, path, body, self.tenant_token())
        if status != 200 or data.get("code") != 0:
            raise RuntimeError(f"Lark {method} {path.split('?')[0]} failed (http {status}, code {data.get('code')})")
        return data

    def _call(self, method: str, path: str, body: dict | None) -> dict:
        status, data = self._http(method, path, body, self.tenant_token())
        if status == 401:
            self._token = ""
            status, data = self._http(method, path, body, self.tenant_token())
        if status != 200 or data.get("code") != 0:
            raise RuntimeError(f"Lark {method} {path.split('?')[0]} failed (http {status}, code {data.get('code')})")
        return data.get("data") or {}

    # ---------------------------------------------------------------- calls

    def bot_open_id(self) -> str:
        """The app's bot's own open_id (to tell a mention of the bot from a
        mention of anyone else); cached for the client's lifetime."""
        if not getattr(self, "_bot_open_id", ""):
            d = self._call_raw("GET", "/bot/v3/info")
            self._bot_open_id = _check_id(str((d.get("bot") or {}).get("open_id", "")), "open_id")
        return self._bot_open_id

    def reply_card(self, message_id: str, content: str, title: str | None = None) -> str:
        """Reply in the message's thread with a card; returns the card message's id."""
        _check_id(message_id, "message_id")
        d = self._call("POST", f"/im/v1/messages/{message_id}/reply",
                       {"msg_type": "interactive", "content": card_json(content, title)})
        return _check_id(d.get("message_id", ""), "message_id")

    def card_id(self, card_message_id: str) -> str:
        _check_id(card_message_id, "message_id")
        d = self._call("POST", "/cardkit/v1/cards/id_convert", {"message_id": card_message_id})
        return _check_id(d.get("card_id", ""), "card_id")

    def update_card(self, card_id: str, content: str, sequence: int) -> None:
        """Replace the card's body. ``sequence`` must strictly increase per card."""
        _check_id(card_id, "card_id")
        self._call("PUT", f"/cardkit/v1/cards/{card_id}",
                   {"card": {"type": "card_json", "data": card_json(content)}, "sequence": int(sequence)})

    def recent_messages(self, chat_id: str, limit: int = 20) -> list[dict]:
        """The chat's latest messages, newest first (needs the app's
        im:message.group_msg scope in a group)."""
        _check_id(chat_id, "chat_id")
        n = max(1, min(int(limit), 50))
        d = self._call("GET", f"/im/v1/messages?container_id_type=chat&container_id={chat_id}"
                              f"&page_size={n}&sort_type=ByCreateTimeDesc", None)
        return list(d.get("items") or [])

    def messages_since(self, chat_id: str, start_ms: int, *, max_messages: int = 200, page_size: int = 50) -> list[dict]:
        """The chat's messages created after ``start_ms``, oldest first, at
        most ``max_messages`` (Lark's start_time is in whole seconds)."""
        _check_id(chat_id, "chat_id")
        out: list[dict] = []
        token = ""
        start_s = max(0, int(start_ms) // 1000)
        while len(out) < max_messages:
            q = (f"/im/v1/messages?container_id_type=chat&container_id={chat_id}&start_time={start_s}"
                 f"&sort_type=ByCreateTimeAsc&page_size={max(1, min(page_size, 50))}")
            if token:
                if not re.fullmatch(r"[A-Za-z0-9_=+/-]{1,512}", token):
                    break
                q += "&page_token=" + urllib.parse.quote(token, safe="")
            d = self._call("GET", q, None)
            items = [i for i in (d.get("items") or []) if int(i.get("create_time") or 0) > int(start_ms)]
            out.extend(items)
            token = d.get("page_token") or ""
            if not d.get("has_more") or not token:
                break
        return out[:max_messages]

    def create_doc(self, title: str, markdown: str) -> str:
        """Create a Docx document filled with ``markdown``; returns its id.
        Lark's three steps: create, convert markdown to blocks, insert them."""
        d = self._call("POST", "/docx/v1/documents", {"title": title[:200]})
        doc_id = _check_id((d.get("document") or {}).get("document_id", ""), "document_id")
        if markdown.strip():
            conv = self._call("POST", "/docx/v1/documents/blocks/convert",
                              {"content_type": "markdown", "content": markdown})
            blocks, first = conv.get("blocks") or [], conv.get("first_level_block_ids") or []
            for b in blocks:
                # the converter returns table merge info the insert does not accept
                prop = (b.get("table") or {}).get("property") if isinstance(b, dict) else None
                if isinstance(prop, dict):
                    prop.pop("merge_info", None)
            if blocks and first:
                self._call("POST", f"/docx/v1/documents/{doc_id}/blocks/{doc_id}/descendant?document_revision_id=-1",
                           {"index": 0, "children_id": first, "descendants": blocks})
        return doc_id

    def share_doc_with_chat(self, doc_id: str, chat_id: str) -> None:
        """Let the chat's members view the document (it is owned by the app)."""
        _check_id(doc_id, "document_id")
        _check_id(chat_id, "chat_id")
        self._call("POST", f"/drive/v1/permissions/{doc_id}/members?type=docx&need_notification=false",
                   {"member_type": "openchat", "member_id": chat_id, "perm": "view"})

    def doc_url(self, doc_id: str) -> str:
        host = self._api.split("//", 1)[1].split("/", 1)[0].replace("open.", "", 1)
        return f"https://{host}/docx/{_check_id(doc_id, 'document_id')}"

    def send_card(self, chat_id: str, content: str, title: str | None = None) -> str:
        """A new card in the chat (a reminder or a scheduled task's answer);
        returns its message id."""
        _check_id(chat_id, "chat_id")
        d = self._call("POST", "/im/v1/messages?receive_id_type=chat_id",
                       {"receive_id": chat_id, "msg_type": "interactive", "content": card_json(content, title)})
        return _check_id(d.get("message_id", ""), "message_id")

    def send_text(self, chat_id: str, text: str) -> None:
        _check_id(chat_id, "chat_id")
        self._call("POST", "/im/v1/messages?receive_id_type=chat_id",
                   {"receive_id": chat_id, "msg_type": "text", "content": json.dumps({"text": text}, ensure_ascii=False)})

    # Lark's file types for an upload; anything else is "stream"
    FILE_TYPES = {"pptx": "ppt", "ppt": "ppt", "docx": "doc", "doc": "doc", "xlsx": "xls", "xls": "xls", "pdf": "pdf"}

    def upload_file(self, name: str, data: bytes) -> str:
        """Upload a file for a message (needs im:resource); returns its file_key."""
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,120}", name or "") or not data:
            raise ValueError("invalid file")
        file_type = self.FILE_TYPES.get(name.rsplit(".", 1)[-1].lower(), "stream")
        boundary = "----lark" + secrets.token_hex(12)
        parts = []
        for key, value in (("file_type", file_type), ("file_name", name)):
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{name}"\r\n'
                     f'Content-Type: application/octet-stream\r\n\r\n'.encode() + data + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        req = urllib.request.Request(self._api + "/im/v1/files", data=b"".join(parts), method="POST")
        req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
        req.add_header("Authorization", f"Bearer {self.tenant_token()}")
        try:
            # under API Gateway's 30 s: a send that outlasts it would be retried
            with self._open(req, timeout=25) as resp:  # nosec B310 - https only, fixed host
                status, body = resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            status, body = e.code, {}
        if status != 200 or body.get("code") != 0:
            raise RuntimeError(f"Lark file upload failed (http {status}, code {body.get('code')})")
        return _check_id(str((body.get("data") or {}).get("file_key", "")), "file_key")

    def send_file(self, chat_id: str, file_key: str) -> None:
        _check_id(chat_id, "chat_id")
        _check_id(file_key, "file_key")
        self._call("POST", "/im/v1/messages?receive_id_type=chat_id",
                   {"receive_id": chat_id, "msg_type": "file", "content": json.dumps({"file_key": file_key})})

    def message_image(self, message_id: str, image_key: str, limit: int) -> bytes:
        """An image of a message the bot received (needs the app's im:resource
        scope); at most ``limit`` bytes."""
        _check_id(message_id, "message_id")
        _check_id(image_key, "image_key")
        return self._download(f"/im/v1/messages/{message_id}/resources/{image_key}?type=image", limit)

    def reply_text(self, message_id: str, text: str) -> None:
        _check_id(message_id, "message_id")
        self._call("POST", f"/im/v1/messages/{message_id}/reply",
                   {"msg_type": "text", "content": json.dumps({"text": text}, ensure_ascii=False)})
