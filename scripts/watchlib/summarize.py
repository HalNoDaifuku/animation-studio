"""サイト監視の変更(urlwatch の差分)を、Gemini API で要約する。

方針:
- エラー通知は要約しない(呼び出し側で、ERROR はここを通さない)。
- 要約は差分1件につき1回だけ。複数のチャンネルに送るときは、同じ要約を使い回す(呼び出し側)。
- 1回の実行での件数に上限がある。上限・失敗・キー未設定のときは、要約なしで差分だけ送る(通知は止めない)。
- 差分は「データ」として渡す。ページの内容に指示のような文があっても従わないようにプロンプトで指示する。
- APIキーはヘッダーで渡し、ログやエラー文には出さない。
"""
from __future__ import annotations

import difflib
import json
import re
import time
import urllib.error
import urllib.request
from typing import Callable

DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com"

DEFAULT_JUDGEMENTS = ("募集開始・追加", "募集終了", "条件・内容の変更", "採用と無関係", "判断できない")
DEFAULT_USER_TEMPLATE = "会社: {company}\nページ: {page}\n差分(追加は+、削除は-。長い場合は先頭のみ):\n{diff}"


def build_system_prompt(options: dict) -> str:
    """config/ai.yaml の instructions と examples から、システムプロンプトを組み立てる。"""
    judgements = options.get("judgements") or list(DEFAULT_JUDGEMENTS)
    text = str(options.get("instructions") or "").rstrip().replace("{judgements}", " / ".join(str(j) for j in judgements))
    for i, ex in enumerate(options.get("examples") or [], 1):
        text += f"\n\n例{i}\n差分:\n{str(ex.get('diff', '')).rstrip(chr(10))}\n出力:\n{str(ex.get('output', '')).rstrip(chr(10))}"
    return text + "\n"


class SummaryError(Exception):
    """要約に失敗した(通知は、要約なしで続ける)。"""


class QuotaExceeded(SummaryError):
    """無料枠の上限(HTTP 429)。この実行では、もう呼ばない。"""


# --- 差分の整形(AIなし) -----------------------------------------------------------------

_SEGMENT_SPLIT = re.compile(r"(?<=[。！？!?])|(?=✦)|(?<=-->)")
_LONG_LINE = 300


def _segments(text: str) -> list[str]:
    return [s for s in _SEGMENT_SPLIT.split(text) if s]


def _inline_changes(old: str, new: str) -> list[str]:
    """1行が非常に長いページ(改行がないページ)は、行ごとの差分だと全部が入れ替わったように見える。
    文の区切りで分けて比べ、変わった部分だけを - / + の行にする。"""
    a, b = _segments(old), _segments(new)
    out: list[str] = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        out += ["-" + s for s in a[i1:i2]] + ["+" + s for s in b[j1:j2]]
    return out


def condense_diff(body: str, *, max_chars: int = 20000) -> tuple[str, bool]:
    """urlwatch の差分を、AIに渡す形にする。戻り値は(本文, 長すぎて切ったか)。
    ヘッダー(--- / +++ / @@)は外し、長い1行の入れ替わりは、変わった部分だけにする。"""
    lines = [ln for ln in body.splitlines() if not (ln.startswith("--- ") or ln.startswith("+++ ") or ln.startswith("@@"))]
    out: list[str] = []
    i = 0
    while i < len(lines):
        if lines[i].startswith("-") and not lines[i].startswith("---"):
            j = i
            while j < len(lines) and lines[j].startswith("-"):
                j += 1
            k = j
            while k < len(lines) and lines[k].startswith("+"):
                k += 1
            removed, added = lines[i:j], lines[j:k]
            if added and any(len(x) > _LONG_LINE for x in removed + added):
                out += _inline_changes("".join(x[1:] for x in removed), "".join(x[1:] for x in added))
            else:
                out += removed + added
            i = k
        else:
            out.append(lines[i])
            i += 1
    text = "\n".join(out).strip("\n")
    if len(text) > max_chars:
        return text[:max_chars] + f"\n(以下省略: 差分が長いため、先頭の{max_chars}文字だけを渡しています)", True
    return text, False


def judgement_of(summary: str | None) -> str:
    """要約の最後の行「判定: ○○」の○○。なければ空。"""
    for line in reversed((summary or "").strip().splitlines()):
        m = re.match(r"\s*判定\s*[:：]\s*(.+?)\s*$", line)
        if m:
            return m.group(1)
    return ""


def added_lines(condensed: str) -> str:
    return "\n".join(ln[1:] for ln in condensed.splitlines() if ln.startswith("+") and not ln.startswith("+++"))


# --- Gemini ------------------------------------------------------------------------------

def _mask(text: str, secret: str) -> str:
    return text.replace(secret, "***") if secret else text


def gemini_summary(user_text: str, *, system_prompt: str, api_key: str, model: str, base_url: str = DEFAULT_BASE_URL, timeout: float = 60,
                   max_output_tokens: int = 2048, opener: Callable = urllib.request.urlopen,
                   sleep: Callable[[float], None] = time.sleep) -> str:
    url = f"{base_url.rstrip('/')}/v1beta/models/{model}:generateContent"
    payload = {
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"role": "user", "parts": [{"text": user_text}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": max_output_tokens},
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    last = "不明なエラー"
    for attempt in (1, 2):
        request = urllib.request.Request(url, data=data, method="POST", headers={
            "Content-Type": "application/json", "x-goog-api-key": api_key, "User-Agent": "animation-studio-watch"})
        try:
            with opener(request, timeout=timeout) as response:
                result = json.loads(response.read().decode("utf-8", "replace"))
            break
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            try:
                message = (json.loads(raw).get("error") or {}).get("message") or raw[:200]
            except ValueError:
                message = raw[:200]
            message = _mask(" ".join(str(message).split())[:200], api_key)
            if e.code == 429:
                raise QuotaExceeded(f"HTTP 429(無料枠の上限): {message}") from None
            last = f"HTTP {e.code}: {message}"
            if e.code >= 500 and attempt == 1:
                sleep(3)
                continue
            raise SummaryError(last) from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = _mask(f"接続できません: {getattr(e, 'reason', e)}", api_key)
            if attempt == 1:
                sleep(3)
                continue
            raise SummaryError(last) from None
        except ValueError:
            raise SummaryError("応答がJSONではありません") from None
    else:
        raise SummaryError(last)
    candidates = result.get("candidates") or []
    if not candidates:
        reason = (result.get("promptFeedback") or {}).get("blockReason")
        raise SummaryError(f"要約が返りませんでした{f'(理由: {reason})' if reason else ''}")
    parts = ((candidates[0].get("content") or {}).get("parts")) or []
    text = "".join(p.get("text", "") for p in parts if isinstance(p, dict)).strip()
    if not text:
        raise SummaryError(f"要約が空でした(finishReason: {candidates[0].get('finishReason')})")
    text = re.sub(r"^```[a-z]*\n|\n```$", "", text).strip()
    return text[:900]


class Summarizer:
    """1回の実行の中で、要約の件数・上限・間隔を管理する。"""

    def __init__(self, options: dict | None, api_key: str | None, *, opener: Callable = urllib.request.urlopen,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
                 log: Callable[[str], None] = print):
        o = options or {}
        self.enabled = bool(o.get("enabled", False))
        self.api_key = (api_key or "").strip()
        self.model = str(o.get("model", "gemini-3.5-flash-lite"))
        self.base_url = str(o.get("base_url", DEFAULT_BASE_URL))
        self.max_per_run = int(o.get("max_per_run", 20))
        self.system_prompt = build_system_prompt(o)
        self.user_template = str(o.get("user_template") or DEFAULT_USER_TEMPLATE)
        if self.enabled and not str(o.get("instructions") or "").strip():
            self.enabled = False
            log("[要約] config/ai.yaml に instructions(AIへの指示)がないため、AI要約は行いません")
        self.max_input_chars = int(o.get("max_input_chars", 20000))
        self.timeout = float(o.get("timeout_seconds", 60))
        self.interval = float(o.get("request_interval_seconds", 4))
        self.max_output_tokens = int(o.get("max_output_tokens", 2048))
        self.opener, self.sleep, self.clock, self.log = opener, sleep, clock, log
        self.used = 0
        self.failed = 0
        self._quota_hit = False
        self._last_call: float | None = None

    def summarize(self, company: str, page: str, body: str) -> tuple[str | None, str, str]:
        """戻り値は(要約, 要約なしの理由, 整形した差分)。要約があれば理由は空。"""
        condensed, truncated = condense_diff(body, max_chars=self.max_input_chars)
        if not self.enabled:
            return None, "", condensed
        if not self.api_key:
            return None, "GEMINI_API_KEY が設定されていないため", condensed
        if self._quota_hit:
            return None, "AIの利用上限(429)に達したため", condensed
        if self.used >= self.max_per_run:
            return None, f"1回の実行での要約上限({self.max_per_run}件)に達したため", condensed
        if self._last_call is not None and self.interval > 0:
            wait = self._last_call + self.interval - self.clock()
            if wait > 0:
                self.sleep(wait)
        self.used += 1
        user_text = self.user_template.format(company=company, page=page, diff=condensed)
        try:
            summary = gemini_summary(user_text, system_prompt=self.system_prompt, api_key=self.api_key, model=self.model, base_url=self.base_url,
                                     timeout=self.timeout, max_output_tokens=self.max_output_tokens,
                                     opener=self.opener, sleep=self.sleep)
        except QuotaExceeded as e:
            self._quota_hit = True
            self.failed += 1
            return None, f"AIの要約に失敗しました({e})", condensed
        except SummaryError as e:
            self.failed += 1
            return None, f"AIの要約に失敗しました({e})", condensed
        finally:
            self._last_call = self.clock()
        if truncated:
            summary += "\n(差分が長いため、先頭部分のみを要約しています)"
        return summary, "", condensed
