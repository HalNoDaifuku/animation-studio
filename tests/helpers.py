"""テスト用の部品: 偽の Discord / RSSHub / Web サーバー(標準ライブラリだけ)と、設定フォルダの作成。"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))


class _Server:
    def __init__(self, handler_factory):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_factory(self))
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


class MockDiscord(_Server):
    """POST /channels/{id}/messages を記録する。fail に {チャンネルID: [ステータス, ...]} を入れると、その順に失敗させる。"""

    def __init__(self):
        self.requests: list[dict] = []
        self.fail: dict[str, list[int]] = {}
        self.guild_channels: list[dict] = []
        self._next_id = 9000000000000000
        server = self

        def factory(_):
            class H(BaseHTTPRequestHandler):
                def log_message(self, *a):
                    pass

                def _reply(self, status, payload, headers=None):
                    raw = json.dumps(payload).encode()
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    for k, v in (headers or {}).items():
                        self.send_header(k, v)
                    self.end_headers()
                    self.wfile.write(raw)

                def do_GET(self):
                    server.requests.append({"method": "GET", "path": self.path, "headers": dict(self.headers), "json": None})
                    self._reply(200, server.guild_channels)

                def do_POST(self):
                    body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                    files = []
                    ctype = self.headers.get("Content-Type", "")
                    if ctype.startswith("multipart/form-data"):   # ファイル添付つき。payload_json と files[n] に分ける
                        boundary = ctype.split("boundary=", 1)[1].encode()
                        data = None
                        for part in body.split(b"--" + boundary):
                            if b"\r\n\r\n" not in part:
                                continue
                            head, content = part.split(b"\r\n\r\n", 1)
                            content = content[:-2] if content.endswith(b"\r\n") else content
                            if b'name="payload_json"' in head:
                                data = json.loads(content)
                            elif b'name="files[' in head:
                                name = head.split(b'filename="', 1)[1].split(b'"', 1)[0].decode("utf-8")
                                files.append((name, content))
                    else:
                        data = json.loads(body) if body else None
                    server.requests.append({"method": "POST", "path": self.path, "headers": dict(self.headers), "json": data, "files": files})
                    parts = self.path.strip("/").split("/")
                    if parts[0] == "channels" and parts[-1] == "messages":
                        queue = server.fail.get(parts[1])
                        if queue:
                            status = queue.pop(0)
                            if status == 429:
                                return self._reply(429, {"message": "rate limited", "retry_after": 0.5})
                            return self._reply(status, {"message": "Must be 2000 or fewer in length." if status == 400 else "err", "code": 50035})
                        return self._reply(200, {"id": "1"})
                    if parts[0] == "guilds" and parts[-1] == "channels":
                        server._next_id += 1
                        created = {"id": str(server._next_id), "name": data["name"], "type": data["type"]}
                        server.guild_channels.append(created)
                        return self._reply(201, created)
                    self._reply(404, {"message": "unknown"})
            return H

        super().__init__(factory)

    def messages(self, channel_id: str | None = None) -> list[dict]:
        out = []
        for r in self.requests:
            parts = r["path"].strip("/").split("/")
            if r["method"] == "POST" and parts[0] == "channels" and parts[-1] == "messages":
                if channel_id is None or parts[1] == channel_id:
                    out.append({"channel": parts[1], **r["json"], "headers": r["headers"], "files": r.get("files", [])})
        return out


class MockWeb(_Server):
    """pages = {"/path": "本文"}。本文は後から書き換えられる。"""

    def __init__(self, pages: dict[str, str] | None = None):
        self.pages = pages or {}
        server = self

        def factory(_):
            class H(BaseHTTPRequestHandler):
                def log_message(self, *a):
                    pass

                def do_GET(self):
                    body = server.pages.get(urlsplit(self.path).path)
                    if body is None:
                        self.send_response(404)
                        self.end_headers()
                        return
                    raw = body.encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
            return H

        super().__init__(factory)


class MockRSSHub(_Server):
    """feeds = {"/twitter/user/Name": [JSON Feed の items]}。受けたURLも記録する。"""

    def __init__(self):
        self.feeds: dict[str, list[dict]] = {}
        self.requested: list[str] = []
        self.require_key: str | None = None
        self.fail_first: dict[str, int] = {}  # {パス: 最初の何回を503にするか}
        server = self

        def factory(_):
            class H(BaseHTTPRequestHandler):
                def log_message(self, *a):
                    pass

                def do_GET(self):
                    server.requested.append(self.path)
                    parts = urlsplit(self.path)
                    if server.require_key and f"key={server.require_key}" not in parts.query:
                        self.send_response(403)
                        self.end_headers()
                        return
                    if server.fail_first.get(parts.path, 0) > 0:
                        server.fail_first[parts.path] -= 1
                        self.send_response(503)
                        self.end_headers()
                        return
                    items = server.feeds.get(parts.path)
                    if items is None:
                        self.send_response(404)
                        self.end_headers()
                        return
                    raw = json.dumps({"version": "https://jsonfeed.org/version/1.1", "items": items}, ensure_ascii=False).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
            return H

        super().__init__(factory)


def make_root(discord_base: str | None = None, *, roles_to_channels: dict | None = None, fallback: str | None = None,
              errors: str | None = None, companies: dict[str, dict] | None = None) -> Path:
    """一時フォルダに config/common.yaml と設定を作る。companies は {ファイル名(拡張子なし): 会社の辞書}。"""
    root = Path(tempfile.mkdtemp(prefix="wc-test-"))
    (root / "config").mkdir()
    shutil.copy(REPO / "config" / "common.yaml", root / "config" / "common.yaml")
    (root / "db" / "companies").mkdir(parents=True)
    (root / "db" / "portals").mkdir()
    for stem, data in (companies or {}).items():
        write_company(root, stem, data)
    discord = {"discord": {"api_base": discord_base or "https://discord.com/api/v10",
                           "channels": {"fallback": fallback, "errors": errors, "roles": roles_to_channels or {}}}}
    (root / "config" / "discord.yaml").write_text(yaml.safe_dump(discord, allow_unicode=True), encoding="utf-8")
    return root


def write_company(root: Path, stem: str, data: dict, folder: str = "companies") -> None:
    (root / "db" / folder / f"{stem}.yaml").write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def load_sample(slug: str) -> dict:
    return yaml.safe_load((REPO / "db" / "companies" / f"{slug}.yaml").read_text(encoding="utf-8"))


class MockGemini(_Server):
    """POST /v1beta/models/{model}:generateContent を記録する。replies に応答を順に入れる(文字列=要約、整数=そのHTTPエラー)。空なら要約を返す。"""

    def __init__(self):
        self.requests: list[dict] = []
        self.replies: list = []
        server = self

        def factory(_):
            class H(BaseHTTPRequestHandler):
                def log_message(self, *a):
                    pass

                def do_POST(self):
                    body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                    server.requests.append({"path": self.path, "headers": dict(self.headers), "json": body})
                    reply = server.replies.pop(0) if server.replies else "追加がありました。\n詳細は原文を確認してください。\n判定: 判断できない"
                    if isinstance(reply, int):
                        raw = json.dumps({"error": {"message": "quota exceeded" if reply == 429 else "err"}}).encode()
                        self.send_response(reply)
                    else:
                        raw = json.dumps({"candidates": [{"content": {"parts": [{"text": reply}]}, "finishReason": "STOP"}]}).encode()
                        self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
            return H

        super().__init__(factory)
