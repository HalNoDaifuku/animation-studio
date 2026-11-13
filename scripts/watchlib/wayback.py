"""Wayback Machine への保存(savepagenow と同じ方法: /save/ を GET して Content-Location を読む)。失敗しても通知は止めない。"""
from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.error
import urllib.request

SAVE_BASE = "https://web.archive.org"
UA = "Mozilla/5.0 (compatible; website-checker/1.0)"


def _keys() -> tuple[str, str]:
    return (os.environ.get("WAYBACK_ACCESS_KEY") or "").strip(), (os.environ.get("WAYBACK_SECRET_KEY") or "").strip()


def capture(url: str, *, base: str = SAVE_BASE, timeout: int = 60, retries: int = 0, wait: float = 10,
            sleep=time.sleep, poll_seconds: float = 90) -> tuple[str, str]:
    """(アーカイブURL, 失敗理由)。成功なら理由は空。失敗したら wait 秒待って retries 回まで取り直す。
    環境変数 WAYBACK_ACCESS_KEY / WAYBACK_SECRET_KEY(archive.org のS3キー)があれば、認証つきの SPN2 API で保存する。
    なければ、従来どおり /save/ を GET する(匿名。GitHub Actions からは断られやすい)。"""
    reason = ""
    access, secret = _keys()
    for attempt in range(retries + 1):
        if attempt:
            sleep(wait)
        if access and secret:
            archive, reason = _capture_spn2(url, base=base, timeout=timeout, access=access, secret=secret,
                                            sleep=sleep, poll_seconds=poll_seconds)
        else:
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


def _json_request(request, timeout: int):
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", "replace"))


def _capture_spn2(url: str, *, base: str, timeout: int, access: str, secret: str, sleep, poll_seconds: float) -> tuple[str, str]:
    """認証つきの Save Page Now 2(POST /save → job_id → /save/status/<job_id> を待つ)。"""
    headers = {"User-Agent": UA, "Accept": "application/json", "Authorization": f"LOW {access}:{secret}"}
    body = urllib.parse.urlencode({"url": url, "capture_all": "1", "if_not_archived_within": "1m"}).encode()
    try:
        job = _json_request(urllib.request.Request(f"{base}/save", data=body, headers=headers, method="POST"), timeout)
        job_id = job.get("job_id")
        if not job_id:
            return "", f"保存を受け付けませんでした({str(job.get('message') or job.get('status_ext') or '')[:80]})"
        waited = 0.0
        while True:
            status = _json_request(urllib.request.Request(f"{base}/save/status/{job_id}", headers=headers), timeout)
            state = status.get("status")
            if state == "success":
                original = status.get("original_url") or url
                return f"{base}/web/{status.get('timestamp')}/{original}", ""
            if state == "error":
                return "", f"保存に失敗({str(status.get('message') or status.get('status_ext') or '')[:80]})"
            if waited >= poll_seconds:
                return "", "保存が時間内に終わりませんでした"
            sleep(5)
            waited += 5
    except urllib.error.HTTPError as e:
        return "", f"HTTP {e.code}"
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return "", f"接続できません: {getattr(e, 'reason', e)}"
    except ValueError:
        return "", "応答がJSONではありません"
