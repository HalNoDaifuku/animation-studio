"""Wayback Machine への保存(savepagenow と同じ方法: /save/ を GET して Content-Location を読む)。失敗しても通知は止めない。"""
from __future__ import annotations

import time
import urllib.error
import urllib.request

SAVE_BASE = "https://web.archive.org"
UA = "Mozilla/5.0 (compatible; website-checker/1.0)"


def capture(url: str, *, base: str = SAVE_BASE, timeout: int = 60, retries: int = 0, wait: float = 10,
            sleep=time.sleep) -> tuple[str, str]:
    """(アーカイブURL, 失敗理由)。成功なら理由は空。失敗したら wait 秒待って retries 回まで取り直す。"""
    reason = ""
    for attempt in range(retries + 1):
        if attempt:
            sleep(wait)
        archive, reason = _capture_once(url, base=base, timeout=timeout)
        if archive:
            return archive, ""
    return "", reason


def _capture_once(url: str, *, base: str, timeout: int) -> tuple[str, str]:
    request = urllib.request.Request(f"{base}/save/{url}", headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            location = response.headers.get("Content-Location") or response.headers.get("Location")
            if location:
                return (location if location.startswith("http") else base + location), ""
            return "", "保存URLを取得できませんでした"
    except urllib.error.HTTPError as e:
        return "", f"HTTP {e.code}"
    except (urllib.error.URLError, TimeoutError) as e:
        return "", f"接続できません: {getattr(e, 'reason', e)}"
