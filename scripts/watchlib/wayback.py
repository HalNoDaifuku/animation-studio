"""Wayback Machine への保存(savepagenow と同じ方法: /save/ を GET して Content-Location を読む)。失敗しても通知は止めない。"""
from __future__ import annotations

import urllib.error
import urllib.request

SAVE_BASE = "https://web.archive.org"
UA = "Mozilla/5.0 (compatible; website-checker/1.0)"


def capture(url: str, *, base: str = SAVE_BASE, timeout: int = 60) -> tuple[str, str]:
    """(アーカイブURL, 失敗理由)。成功なら理由は空。"""
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
