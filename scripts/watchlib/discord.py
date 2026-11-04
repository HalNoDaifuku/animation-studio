"""Discord Bot での送信(Webhookではなく Bot API)。標準ライブラリだけで動く。

Bot トークンは環境変数 DISCORD_BOT_TOKEN で渡す。ログやエラーメッセージには出さない。
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from typing import Any, Callable

from .messages import DISCORD_LIMIT, truncate

DEFAULT_API_BASE = "https://discord.com/api/v10"
# Discord は User-Agent を「DiscordBot (URL, バージョン)」の形に求める。
# Python 標準の User-Agent だと、Cloudflare に弾かれる(エラー1010)ことがある。
USER_AGENT = "DiscordBot (https://github.com/HalNoDaifuku/website-checker, 1.0)"

SUPPRESS_EMBEDS = 1 << 2  # URLのプレビューを出さない


class DiscordError(Exception):
    def __init__(self, status: int, message: str, code: int | None = None):
        detail = f"Discord API HTTP {status}"
        if code:
            detail += f" (code {code})"
        super().__init__(f"{detail}: {message}")
        self.status = status
        self.code = code


def _safe_json(raw: str) -> dict:
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


class DiscordBot:
    def __init__(
        self,
        token: str,
        api_base: str = DEFAULT_API_BASE,
        *,
        timeout: int = 30,
        max_retries: int = 4,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if not token:
            raise ValueError("Bot トークンが空です")
        self._token = token
        self.api_base = api_base.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.sleep = sleep

    def __repr__(self) -> str:  # トークンを表示しない
        return f"DiscordBot(api_base={self.api_base!r})"

    # --- 低レベル -------------------------------------------------------------------
    def _request(self, method: str, path: str, body: Any = None) -> Any:
        url = self.api_base + path
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        for attempt in range(1, self.max_retries + 1):
            request = urllib.request.Request(
                url,
                data=data,
                method=method,
                headers={
                    "Authorization": f"Bot {self._token}",
                    "Content-Type": "application/json",
                    "User-Agent": USER_AGENT,
                },
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    raw = response.read().decode("utf-8", "replace")
                    self._respect_rate_limit(response.headers)
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as e:
                raw = e.read().decode("utf-8", "replace")
                payload = _safe_json(raw)
                if e.code == 429 and attempt < self.max_retries:
                    wait = payload.get("retry_after") or e.headers.get("Retry-After") or 1
                    self.sleep(min(float(wait) + 0.1, 60))
                    continue
                if e.code >= 500 and attempt < self.max_retries:
                    self.sleep(2 ** (attempt - 1))
                    continue
                raise DiscordError(e.code, str(payload.get("message") or raw[:200]), payload.get("code")) from None
            except urllib.error.URLError as e:
                if attempt < self.max_retries:
                    self.sleep(2 ** (attempt - 1))
                    continue
                raise DiscordError(0, f"接続できません: {e.reason}") from None
        raise DiscordError(0, "リトライ上限に達しました")  # 通常は到達しない

    def _respect_rate_limit(self, headers) -> None:
        """残りが0なら、リセットまで待つ(次のリクエストで429にならないように)。"""
        try:
            if headers.get("X-RateLimit-Remaining") == "0":
                self.sleep(min(float(headers.get("X-RateLimit-Reset-After") or 1), 10))
        except (TypeError, ValueError):
            pass

    # --- メッセージ -----------------------------------------------------------------
    def send_message(self, channel_id: str, content: str, *, suppress_embeds: bool = False) -> dict:
        if not re.fullmatch(r"\d+", str(channel_id)):
            raise DiscordError(0, f"チャンネルIDが数字ではありません: {channel_id!r}")
        body: dict[str, Any] = {
            "content": truncate(content, DISCORD_LIMIT),
            "allowed_mentions": {"parse": []},  # @everyone などを発火させない
        }
        if suppress_embeds:
            body["flags"] = SUPPRESS_EMBEDS
        return self._request("POST", f"/channels/{channel_id}/messages", body)

    # --- チャンネル管理(discord_setup_channels.py 用) -------------------------------
    def list_guild_channels(self, guild_id: str) -> list[dict]:
        return self._request("GET", f"/guilds/{guild_id}/channels")

    def create_guild_channel(self, guild_id: str, payload: dict) -> dict:
        return self._request("POST", f"/guilds/{guild_id}/channels", payload)


class DryRunBot:
    """送信せずに、内容を表示するだけ(--dry-run 用)。トークンも不要。"""

    def __init__(self, out: Callable[[str], None] = print):
        self.out = out
        self.sent: list[tuple[str, str]] = []

    def send_message(self, channel_id: str, content: str, *, suppress_embeds: bool = False) -> dict:
        self.sent.append((str(channel_id), content))
        self.out(f"[dry-run] → チャンネル {channel_id}\n{content}\n{'-' * 40}")
        return {}


def make_bot(cfg, token: str) -> "DiscordBot":
    """common.yaml の network.discord(タイムアウト・再試行回数)を反映した Bot を作る。"""
    n = (cfg.network.get("discord") or {})
    return DiscordBot(token, cfg.discord_channels["api_base"], timeout=int(n.get("timeout_seconds", 30)),
                      max_retries=int(n.get("max_retries", 4)))
