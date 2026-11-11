"""RSSHub フィードの「既読」状態(JSON)。初回は通知せず、既読にするだけ(過去分の洪水を防ぐ)。"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

MAX_IDS_PER_FEED = 500


class State:
    def __init__(self, path: Path | None):
        self.path = Path(path) if path else None
        self.feeds: dict[str, list[str]] = {}
        self.channels: dict[str, str] = {}  # YouTube の @ID → チャンネルID(調べ直さないための控え)
        self.fetched: dict[str, float] = {}  # キー → 最後に取得できた時刻(UNIX秒)。古いものから先に取得するため
        self.errors: dict[str, float] = {}  # キー → 最後にエラー通知を送った時刻(UNIX秒)
        if self.path and self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self.feeds = {k: list(v) for k, v in (data.get("feeds") or {}).items()}
                self.errors = {k: float(v) for k, v in (data.get("errors") or {}).items()}
                self.channels = {k: str(v) for k, v in (data.get("channels") or {}).items()}
                self.fetched = {k: float(v) for k, v in (data.get("fetched") or {}).items()}
            except (ValueError, OSError, AttributeError):
                self.feeds, self.errors, self.channels, self.fetched = {}, {}, {}, {}  # 壊れていたら初回扱い(通知は出さず既読にする)

    def initialized(self, key: str) -> bool:
        return key in self.feeds

    def seen(self, key: str, item_id: str) -> bool:
        return item_id in self.feeds.get(key, [])

    def mark(self, key: str, item_ids: list[str]) -> None:
        ids = self.feeds.setdefault(key, [])
        for i in item_ids:
            if i not in ids:
                ids.append(i)
        del ids[:-MAX_IDS_PER_FEED]

    # --- 同じエラーを毎回通知しない -------------------------------------------------------------
    def error_due(self, key: str, hours: float, now: float | None = None) -> bool:
        """このキーのエラーを(今)通知してよいか。前回の通知から hours 時間たつまでは False。"""
        last = self.errors.get(key)
        return last is None or hours <= 0 or ((now if now is not None else time.time()) - last) >= hours * 3600

    def mark_error(self, key: str, now: float | None = None) -> None:
        self.errors[key] = now if now is not None else time.time()

    def mark_fetched(self, key: str, now: float | None = None) -> None:
        self.fetched[key] = now if now is not None else time.time()

    def clear_error(self, key: str) -> None:
        self.errors.pop(key, None)

    def save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps({"feeds": self.feeds, "errors": self.errors, "channels": self.channels, "fetched": self.fetched}, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)
