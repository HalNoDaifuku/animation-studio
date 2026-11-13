"""SNSの投稿が「就活(採用・応募)に関係あるか」を、Gemini で判定する(要約はしない)。

方針:
- 判定できなかったとき(キー未設定・上限・失敗・読めない応答)は「関係あり」として、今までどおりの送り先に送る(通知を隠さない)。
- 関係なしと判定されたものだけを、別のチャンネル(discord.channels.irrelevant.sns)へ送る。チャンネルが未設定なら、判定自体を行わない。
- 1回の実行での件数に上限がある。APIキーはヘッダーで渡し、ログやエラー文には出さない。
"""
from __future__ import annotations

import re
import time
import urllib.request
from typing import Callable

from .summarize import DEFAULT_BASE_URL, QuotaExceeded, SummaryError, gemini_summary

DEFAULT_USER_TEMPLATE = "会社: {company}\n投稿:\n{text}"
YES, NO = "関係あり", "関係なし"
_VERDICT = re.compile(r"判定\s*[:：]\s*(関係あり|関係なし)")
_REASON = re.compile(r"理由\s*[:：]\s*(.+)")


def build_prompt(options: dict) -> str:
    text = str(options.get("instructions") or "").rstrip()
    for i, ex in enumerate(options.get("examples") or [], 1):
        text += f"\n\n例{i}\n投稿:\n{str(ex.get('text', '')).rstrip(chr(10))}\n出力:\n{str(ex.get('output', '')).rstrip(chr(10))}"
    return text + "\n"


def parse_verdict(answer: str) -> tuple[bool | None, str]:
    """AIの応答から、(関係あるか, 理由)を取り出す。読めなければ (None, "")。"""
    m = _VERDICT.search(answer or "")
    if not m:
        return None, ""
    reason = _REASON.search(answer)
    return m.group(1) == YES, (reason.group(1).strip()[:80] if reason else "")


class Classifier:
    def __init__(self, options: dict | None, api_key: str | None, *, opener: Callable = urllib.request.urlopen,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
                 log: Callable[[str], None] = print):
        o = options or {}
        self.enabled = bool(o.get("enabled", False))
        self.api_key = (api_key or "").strip()
        self.model = str(o.get("model", "gemini-3.5-flash-lite"))
        self.base_url = str(o.get("base_url", DEFAULT_BASE_URL))
        self.max_per_run = int(o.get("max_per_run", 60))
        self.max_input_chars = int(o.get("max_input_chars", 1500))
        self.timeout = float(o.get("timeout_seconds", 60))
        self.interval = float(o.get("request_interval_seconds", 4))
        self.max_output_tokens = int(o.get("max_output_tokens", 512))
        self.system_prompt = build_prompt(o)
        self.user_template = str(o.get("user_template") or DEFAULT_USER_TEMPLATE)
        if self.enabled and not str(o.get("instructions") or "").strip():
            self.enabled = False
            log("[関連判定] config/ai.yaml の relevance.instructions がないため、AI判定は行いません")
        self.opener, self.sleep, self.clock, self.log = opener, sleep, clock, log
        self.used = 0
        self.failed = 0
        self.irrelevant = 0
        self._quota_hit = False
        self._last_call: float | None = None

    @property
    def active(self) -> bool:
        return self.enabled and bool(self.api_key)

    def judge(self, company: str, text: str) -> tuple[bool | None, str]:
        """戻り値は(関係あるか, 理由 または 判定できなかった理由)。判定できなければ関係あるか=None(呼び出し側は「関係あり」として扱う)。"""
        if not self.enabled:
            return None, ""
        if not self.api_key:
            return None, "GEMINI_API_KEY が未設定"
        if self._quota_hit:
            return None, "AIの利用上限(429)に達した"
        if self.used >= self.max_per_run:
            return None, f"1回の実行での判定上限({self.max_per_run}件)に達した"
        if self._last_call is not None and self.interval > 0:
            wait = self._last_call + self.interval - self.clock()
            if wait > 0:
                self.sleep(wait)
        self.used += 1
        body = text.strip()
        if len(body) > self.max_input_chars:
            body = body[: self.max_input_chars] + "\n(以下省略)"
        user_text = self.user_template.format(company=company, text=body)
        try:
            answer = gemini_summary(user_text, system_prompt=self.system_prompt, api_key=self.api_key, model=self.model,
                                    base_url=self.base_url, timeout=self.timeout, max_output_tokens=self.max_output_tokens,
                                    opener=self.opener, sleep=self.sleep)
        except QuotaExceeded as e:
            self._quota_hit = True
            self.failed += 1
            return None, f"AIの判定に失敗({e})"
        except SummaryError as e:
            self.failed += 1
            return None, f"AIの判定に失敗({e})"
        finally:
            self._last_call = self.clock()
        verdict, reason = parse_verdict(answer)
        if verdict is None:
            self.failed += 1
            return None, "AIの応答を読めませんでした"
        if not verdict:
            self.irrelevant += 1
        return verdict, reason
