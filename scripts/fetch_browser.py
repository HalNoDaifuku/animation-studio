"""ブラウザ(Playwright の Chromium)でページを開き、表示された後の HTML を標準出力に出す。

使いどころ: urlwatch の通常の取得(requests)が、403 や「bot対策の確認ページ」で断られるサイト用。
会社ファイルの monitor に `fetch: {mode: browser}` と書くと、urlwatch の command ジョブとして、このスクリプトが使われる。

- 実在のブラウザと同じ User-Agent・言語・タイムゾーンで開く(headless を名乗らない)。
- 確認ページが出たら、数秒待って読み直す(JavaScript で自動的に通過するタイプ)。
- 失敗(接続できない・HTTP 4xx/5xx・確認ページのまま)は、終了コード 1 と、標準エラー出力のメッセージで知らせる(urlwatch がエラーとして報告する)。
- ネットワーク側(サイトがクラウドのIPからの接続を断っている)の問題は、ブラウザにしても直らない。
"""
from __future__ import annotations

import argparse
import sys
import time

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/131.0.0.0 Safari/537.36")
CHALLENGE_MARKERS = ("Checking your browser", "Just a moment", "cf-browser-verification", "ブラウザを確認", "Access denied")


def looks_like_challenge(html: str) -> bool:
    head = html[:6000]
    return any(m in head for m in CHALLENGE_MARKERS)


def fetch(url: str, *, timeout: float, retries: int, sleep_seconds: float, wait_selector: str | None, log=print) -> str:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import TimeoutError as PlaywrightTimeout
    from playwright.sync_api import sync_playwright

    last = "不明なエラー"
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        try:
            context = browser.new_context(user_agent=USER_AGENT, locale="ja-JP", timezone_id="Asia/Tokyo",
                                          viewport={"width": 1366, "height": 900},
                                          extra_http_headers={"Accept-Language": "ja,en-US;q=0.8,en;q=0.6"})
            page = context.new_page()
            page.set_default_timeout(timeout * 1000)
            for attempt in range(retries + 1):
                if attempt:
                    time.sleep(sleep_seconds)
                try:
                    response = page.goto(url, wait_until="domcontentloaded")
                    if wait_selector:
                        page.wait_for_selector(wait_selector, timeout=timeout * 1000)
                    else:
                        try:
                            page.wait_for_load_state("networkidle", timeout=min(timeout, 15) * 1000)
                        except PlaywrightTimeout:
                            pass   # 広告などで通信が止まらないページ。ここまでの内容を使う
                    html = page.content()
                    for _ in range(3):   # 確認ページなら、通過するのを待って読み直す
                        if not looks_like_challenge(html):
                            break
                        time.sleep(5)
                        html = page.content()
                    status = response.status if response else 0
                    if status >= 400:
                        last = f"HTTP {status}"
                        continue
                    if looks_like_challenge(html):
                        last = "bot対策の確認ページのままでした"
                        continue
                    return html
                except PlaywrightTimeout:
                    last = "時間内に読み込めませんでした(タイムアウト)"
                except PlaywrightError as e:
                    last = " ".join(str(e).split())[:200]
        finally:
            browser.close()
    raise RuntimeError(last)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--timeout", type=float, default=45)
    ap.add_argument("--retries", type=int, default=1)
    ap.add_argument("--sleep", type=float, default=5)
    ap.add_argument("--wait-selector", default=None)
    a = ap.parse_args(argv)
    try:
        html = fetch(a.url, timeout=a.timeout, retries=a.retries, sleep_seconds=a.sleep, wait_selector=a.wait_selector)
    except Exception as e:  # noqa: BLE001 - 終了コードとメッセージで知らせる
        print(f"ブラウザでの取得に失敗しました: {e}", file=sys.stderr)
        return 1
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stdout.write(html)
    return 0


if __name__ == "__main__":
    sys.exit(main())
