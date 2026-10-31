"""RSSHub フィードの「既読」状態(JSON)。初回は通知せず、既読にするだけ(過去分の洪水を防ぐ)。"""
from __future__ import annotations

import json
import os
from pathlib import Path

MAX_IDS_PER_FEED = 500


class State:
    def __init__(self, path: Path | None):
        self.path = Path(path) if path else None
        self.feeds: dict[str, list[str]] = {}
        if self.path and self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self.feeds = {k: list(v) for k, v in (data.get("feeds") or {}).items()}
            except (ValueError, OSError, AttributeError):
                self.feeds = {}  # 壊れていたら初回扱い(通知は出さず既読にする)

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

    def save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps({"feeds": self.feeds}, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)
