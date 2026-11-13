"""RSSHub通知・urlwatch通知の本体(CLIから呼ぶ。テストしやすいよう、副作用は引数で受け取る)。"""
from __future__ import annotations

import re
import subprocess
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .config import Config
from .discord import DiscordError
from .messages import (build_diff_message, build_error_message, build_summary_message, build_feed_error_message, build_sns_message,
                       split_long_text, CHUNK_LIMIT, JST)
from .report import ReportEntry, parse_report
from .routing import Target, pick_roles, resolve_channels
from .rsshub import Feed, FeedFetchError, fetch_feed, fetch_youtube, fetch_youtube_api, is_retweet
from .state import State
from .urlwatch_jobs import JobInfo
from . import wayback as wayback_mod

def probe_url(url: str, timeout: int = 20) -> str:  # 既定値。実際は common.yaml の network.probe_timeout_seconds
    """URLを1回だけ取得して、状態を短い文字列で返す(報告に載らなかったジョブの原因調べ用)。"""
    import urllib.error
    import urllib.request
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; website-checker/1.0)"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return f"HTTP {response.status}({len(response.read())}バイト)"
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code}"
    except (urllib.error.URLError, TimeoutError) as e:
        return f"接続できません: {getattr(e, 'reason', e)}"


MAX_DIFF_MESSAGES = 8  # 1件の差分を、1チャンネルへ何通まで分割して送るか


@dataclass
class RunResult:
    sent: int = 0
    skipped: int = 0
    send_failures: list[str] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)  # 取得エラーなど(通知はするが失敗扱いにはしない)

    @property
    def failed(self) -> bool:
        return bool(self.send_failures)


def added_text(body: str) -> str:
    """urlwatch の差分から、増えた行(+ の行)だけを取り出す。差分の形でなければ、本文全体。"""
    lines = body.splitlines()
    added = [ln[1:] for ln in lines if ln.startswith("+") and not ln.startswith("+++")]
    return "\n".join(added) if added else body


def changed_text(body: str) -> str:
    """送信先の判定に使う文。増えた行(+)と、消えた行(-)の両方。差分の形でなければ、本文全体。"""
    lines = body.splitlines()
    changed = [ln[1:] for ln in lines if (ln.startswith("+") and not ln.startswith("+++")) or (ln.startswith("-") and not ln.startswith("---"))]
    return "\n".join(changed) if changed else body


def _safe(text, limit: int = 160) -> str:
    """ログ用に1行へ縮める。"::" は GitHub Actions のコマンドと解釈されるので変える(外部ページの本文が混ざるため)。"""
    raw = str(text or "")
    if "Traceback (most recent call last)" in raw:   # 長いスタックトレースは、原因が書いてある最後の行だけ残す
        last = [ln.strip() for ln in raw.splitlines() if ln.strip()][-1]
        raw = f"例外: {last}"
    one = " ".join(raw.split()).replace("::", ": :")
    return one if len(one) <= limit else one[:limit] + "…"


def _mask(channel_id: str) -> str:
    """ログにチャンネルIDをそのまま出さない(末尾4桁だけ)。"""
    cid = str(channel_id)
    return "ch…" + cid[-4:] if cid.isdigit() else cid


def _where(targets: list) -> str:
    names = []
    for t in targets:
        names.append("fallback" if getattr(t, "is_fallback", False) else (",".join(t.roles) or _mask(t.channel_id)))
    return " / ".join(names) or "(送り先なし)"


def _targets(cfg: Config, company: dict, hint_roles, text, dry_run: bool) -> list[Target]:
    roles = pick_roles(cfg, company, hint_roles=hint_roles, text=text)
    targets = resolve_channels(roles, cfg.discord_channels, company.get("type"))
    if not targets and dry_run:
        targets = [Target("(未設定)", roles)]
    return targets


def send_to_errors_channel(cfg: Config, bot, content: str, result: RunResult, *, dry_run: bool = False) -> None:
    """運用エラーの送り先。errors → fallback の順。どちらも無ければ表示だけ(ジョブは失敗にしない)。"""
    ch = cfg.discord_channels
    target = ch.get("errors") or ch.get("fallback") or ("(未設定)" if dry_run else None)
    if not target:
        print("[注意] errors / fallback チャンネルが未設定のため、次のエラーは Discord に送れません:\n" + content)
        return
    try:
        bot.send_message(target, content, suppress_embeds=True)
        result.sent += 1
    except DiscordError as e:
        result.send_failures.append(f"errors: {e}")


# --- RSSHub ----------------------------------------------------------------------------------

def _parse_published(value: str | None):
    """date_published を UTC の datetime にする。読めなければ None(その場合は古さで除外しない)。"""
    if not value:
        return None
    text = str(value).strip()
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _process_feed(cfg: Config, feed: Feed, items: list, state: State, bot, result: RunResult, company: dict, *,
                  dry_run: bool, notify_on_first_run: bool, limit: int, log: Callable[[str], None]) -> None:
    first_run = not state.initialized(feed.key)
    if first_run and not notify_on_first_run:
        if not dry_run:
            state.mark(feed.key, [i.id for i in items])
            state.save()
        log(f"[初回] {feed.key}: {len(items)}件を既読にしました(通知なし)")
        return
    new = [i for i in items if not state.seen(feed.key, i.id)]
    max_age = float((cfg.sns.get("fetch") or {}).get("max_age_days", 30) or 0)
    if max_age > 0 and new:
        cutoff = datetime.now(timezone.utc) - timedelta(days=max_age)
        old = [i for i in new if (_parse_published(i.published) or datetime.max.replace(tzinfo=timezone.utc)) < cutoff]
        if old:
            if not dry_run:
                state.mark(feed.key, [i.id for i in old])  # 古い投稿は通知せず既読にする
            new = [i for i in new if i not in old]
            log(f"[古い投稿を除外] {feed.key}: {len(old)}件(公開から{max_age:g}日より前)")
    if cfg.sns.get("exclude_retweets", True):
        retweets = [i for i in new if is_retweet(i.text)]
        if retweets:
            if not dry_run:
                state.mark(feed.key, [i.id for i in retweets])  # 次回また判定しないよう既読にする
            new = [i for i in new if not is_retweet(i.text)]
    if not new:
        if not dry_run:
            state.save()
        return
    new.reverse()  # 古い順に送る
    if len(new) > limit:
        result.skipped += len(new) - limit
        log(f"[上限] {feed.key}: 新着{len(new)}件のうち、新しい{limit}件だけ通知します")
        skipped_ids = [i.id for i in new[:-limit]]
        new = new[-limit:]
        if not dry_run:
            state.mark(feed.key, skipped_ids)
    for item in new:
        text = build_sns_message(company_name=feed.company_name, platform_label=feed.platform_label,
                                 account_label=feed.label, text=item.text, url=item.url,
                                 published=item.published)
        targets = _targets(cfg, company, feed.hint_roles, item.text, dry_run)
        if not targets:
            result.send_failures.append(f"{feed.key}: 送り先チャンネルがありません(config/discord.yaml を確認)")
            continue
        ok = True
        log(f"[通知] {feed.company_name} / {feed.platform_label} / {feed.label} → {_where(targets)}"
            f"\n    {item.published or '日時不明'} {item.url}\n    {_safe(item.text, 200)}")
        for t in targets:
            try:
                bot.send_message(t.channel_id, text, suppress_embeds=False)
                result.sent += 1
            except DiscordError as e:
                ok = False
                result.send_failures.append(f"{feed.key} → {_mask(t.channel_id)}: {e}")
        if ok and not dry_run:
            state.mark(feed.key, [item.id])  # 送れなかったものは既読にせず、次回また試す
    if not dry_run:
        state.save()


def _fetch_all(feeds: list, fetch_one, workers: int, interval: float, sleep, clock):
    """feeds を取得し、(feed, items, error, 秒数) を feeds と同じ順番で返す。

    取得(通信)だけを workers 本で並行して行い、結果の処理(状態の更新・通知)は呼び出し側が順番に行う。
    要求を始める間隔は、並行していても interval 秒以上あける(相手に一斉に要求しないため)。
    """
    import threading
    from concurrent.futures import ThreadPoolExecutor

    gate = threading.Lock()
    last_start = [None]

    def one(feed):
        with gate:  # 要求の開始間隔をあける
            if last_start[0] is not None and interval > 0:
                wait = last_start[0] + interval - clock()
                if wait > 0:
                    sleep(wait)
            last_start[0] = clock()
        began = clock()
        try:
            return feed, fetch_one(feed), None, clock() - began
        except FeedFetchError as e:
            return feed, None, e, clock() - began

    if workers <= 1:
        for feed in feeds:
            yield one(feed)
        return
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(one, f) for f in feeds]
        for fut in futures:
            yield fut.result()


def run_rsshub(cfg: Config, feeds: list[Feed], state: State, bot, *, dry_run: bool = False,
               notify_on_first_run: bool = False, limit: int = 5,
               fetch: Callable[[str], list] | None = None, build_errors: list[tuple[str, str]] | None = None,
               retry_rounds: int | None = None, retry_wait: float | None = None,
               request_interval: float | None = None, error_renotify_hours: float | None = None,
               sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
               youtube_opener: Callable | None = None, youtube_api_key: str | None = None,
               log: Callable[[str], None] = print) -> RunResult:
    """全フィードを1回取得し、失敗したものだけを「全部が終わってから」待って再取得する(retry_rounds 回まで)。

    すぐ再試行すると、同じ相手へ続けて要求することになるため、まとめて後回しにする。
    それでも失敗したものだけをエラーとして通知し、同じエラーは error_renotify_hours 時間は繰り返さない。
    """
    opts = cfg.sns.get("fetch") or {}
    rounds = int(opts.get("retry_rounds", 2) if retry_rounds is None else retry_rounds)
    wait = float(opts.get("retry_wait_seconds", 120) if retry_wait is None else retry_wait)
    interval = float(opts.get("request_interval_seconds", 0.5) if request_interval is None else request_interval)
    renotify = float(opts.get("error_renotify_hours", 24) if error_renotify_hours is None else error_renotify_hours)
    timeout = int(opts.get("timeout_seconds", 30))
    budget = float(opts.get("retry_time_budget_seconds", 600))
    if fetch is None:
        fetch = lambda url: fetch_feed(url, timeout=timeout)  # noqa: E731
    workers = max(1, int(opts.get("concurrency", 1)))
    yt_timeout = int((opts.get("youtube") or {}).get("timeout_seconds", timeout))

    import itertools
    rr = itertools.count()                       # 交互に使うための通し番号
    stats: dict[int, list[int]] = {}             # インスタンスごとの [要求数, 失敗数, 別のインスタンスから切り替えて成功した数]
    n_instances = 1 + max((len(f.alt_urls) for f in feeds), default=0)
    if n_instances > 1:
        stats = {i: [0, 0, 0] for i in range(n_instances)}

    def fetch_one(feed: Feed) -> list:
        if feed.direct == "youtube_api":   # RSSHub を通さず、YouTube Data API(公式)で取得
            return fetch_youtube_api(feed, youtube_api_key, resolved=state.channels, timeout=yt_timeout, opener=youtube_opener)
        if feed.direct == "youtube":   # RSSHub を通さず、YouTube の公式フィードを直接読む
            return fetch_youtube(feed, resolved=state.channels, timeout=yt_timeout, opener=youtube_opener)
        urls = [feed.request_url] + list(feed.alt_urls)
        if len(urls) == 1:
            return fetch(urls[0])
        # 複数の RSSHub を交互に使う(要求ごとに、最初に使うものを順番に変える)。失敗したら、残りのインスタンスで取り直す。
        start = next(rr) % len(urls)
        order = [(start + k) % len(urls) for k in range(len(urls))]
        problems: dict[int, str] = {}
        for attempt, idx in enumerate(order):
            stats[idx][0] += 1
            try:
                items = fetch(urls[idx])
            except FeedFetchError as e:
                stats[idx][1] += 1
                problems[idx] = str(e)
                continue
            if attempt:
                stats[idx][2] += 1
                log(f"[切替] {feed.key}: RSSHub {order[0] + 1}つ目で失敗({_safe(problems[order[0]], 60)})→{idx + 1}つ目で取得しました")
            return items
        raise FeedFetchError(" / ".join(f"RSSHub {i + 1}つ目: {problems[i]}" for i in sorted(problems)))

    rl = opts.get("rate_limit") or {}
    rl_on = bool(rl.get("enabled", True))
    rl_chunk = max(1, int(rl.get("chunk_size", workers * 4)))
    rl_waits = int(rl.get("waits", 1))
    rl_wait_s = float(rl.get("wait_seconds", 540))
    rl_ratio = float(rl.get("failure_ratio", 0.9))
    skipped: list[Feed] = []

    def first_pass(todo: list[Feed]):
        """最初の取得。X(RSSHub)が短時間に多すぎる要求を制限すると、ほぼ全部がタイムアウトになる。
        そこで少しずつ取得し、ほぼ全滅したら制限とみなして待つ。待っても続くなら、残りは次回に回す(古いものから先に取る)。"""
        queue = list(todo)
        waits_left = rl_waits
        while queue:
            chunk, queue = queue[:rl_chunk], queue[rl_chunk:]
            got = list(_fetch_all(chunk, fetch_one, workers, interval, sleep, clock))
            probes = [g for g in got if not g[0].direct]   # YouTube 公式APIは、この制限とは別
            limited = [g for g in probes if g[2] is not None and str(g[2]).count("タイムアウト") >= 1 + len(g[0].alt_urls)]   # 全インスタンスがタイムアウト
            if rl_on and len(probes) >= 4 and len(limited) >= rl_ratio * len(probes):
                log(f"[制限の疑い] {len(probes)}件中{len(limited)}件がタイムアウトしました(RSSHub/X の要求制限の可能性)")
                retry = [g[0] for g in got if g[2] is not None and g[0] in [x[0] for x in limited]]
                if waits_left > 0:
                    waits_left -= 1
                    log(f"[制限の疑い] {rl_wait_s:g}秒待ってから続けます(残り{waits_left}回)")
                    sleep(rl_wait_s)
                    got = [g for g in got if g[0] not in retry]
                    queue = retry + queue
                else:
                    log(f"[制限の疑い] 待っても改善しないため、残り{len(retry) + len(queue)}件は次回に回します(古いものから優先して取得します)")
                    got = [g for g in got if g[0] not in retry]
                    skipped.extend(retry + queue)
                    queue = []
            yield from got

    result = RunResult(errors=[])
    last_error: dict[str, str] = {}
    pending = sorted(feeds, key=lambda f: state.fetched.get(f.key, 0.0))   # 最後に取得できてから長いものを先に
    still_failed: list[Feed] = []
    started = clock()
    for attempt in range(rounds + 1):
        if attempt:
            if attempt == 1:  # 前回から続けて失敗している(エラー通知済みの)ものは、取り直さない
                known = [f for f in pending if f.key in state.errors]
                if known:
                    log(f"[再試行しない] 前回から続けて失敗しているため: {len(known)}件")
                    pending = [f for f in pending if f.key not in state.errors]
                    still_failed.extend(known)
            if not pending:
                break
            if clock() - started + wait > budget:
                log(f"[再試行を打ち切り] 取得にかかった時間が {budget:g}秒(sns.fetch.retry_time_budget_seconds)を超えるため、残り{len(pending)}件は取り直しません")
                break
            log(f"[再試行 {attempt}/{rounds}] 取得に失敗した{len(pending)}件を、{wait:g}秒待ってから取得し直します")
            sleep(wait)
        failed: list[Feed] = []
        for feed, items, err, took in (first_pass(pending) if attempt == 0 else _fetch_all(pending, fetch_one, workers, interval, sleep, clock)):
            if err is not None:
                last_error[feed.key] = str(err)
                failed.append(feed)
                log(f"[取得失敗] {feed.key}: {err}" + (f"(試行{attempt + 1}回目)" if rounds else "") + f" / {took:.1f}秒")
                continue
            if not dry_run:
                state.clear_error(feed.key)
                state.mark_fetched(feed.key)
            who = f" / チャンネル名「{_safe(feed.channel_title, 60)}」(会社: {feed.company_name})" if feed.channel_title else ""
            log(f"[取得] {feed.key}: {len(items)}件{who} / {took:.1f}秒")
            _process_feed(cfg, feed, items, state, bot, result, cfg.company(feed.company_slug) or {},
                          dry_run=dry_run, notify_on_first_run=notify_on_first_run, limit=limit, log=log)
        pending = failed

    if stats:
        log("[RSSHub] " + " / ".join(f"{i + 1}つ目: 要求{s[0]}件・失敗{s[1]}件・他から切り替えて成功{s[2]}件" for i, s in sorted(stats.items())))
    unresolved = [(f.key, last_error[f.key]) for f in pending + still_failed] + list(build_errors or [])
    if skipped:   # 個別のエラーにはせず、1件にまとめて必ず知らせる
        unresolved.append(("RSSHub/X の要求制限(疑い)", f"{len(skipped)}件を今回は取得できませんでした。次回の実行で、取得してから長いものを優先して取り直します"))
    to_report = []
    for key, reason in unresolved:
        if dry_run or state.error_due(key, renotify):
            to_report.append((key, reason))
            if not dry_run:
                state.mark_error(key)
        else:
            log(f"[エラー通知を省略] {key}: {reason}(前回の通知から{renotify:g}時間以内)")
    result.errors = unresolved  # 終了時の件数表示には、省略した分も含める
    if not dry_run:
        state.save()
    if to_report:
        for key, reason in to_report:
            log(f"[エラー通知] {key}: {_safe(reason)}")
        send_to_errors_channel(cfg, bot, build_feed_error_message(to_report), result, dry_run=dry_run)
    return result


# --- urlwatch --------------------------------------------------------------------------------

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def run_urlwatch_command(urls_path: Path, cache_path: Path, command: str = "urlwatch",
                         timeout: int = 1800, hooks: Path | None = None) -> tuple[int, str, str]:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    extra = ["--hooks", str(hooks)] if hooks else []
    proc = subprocess.run([command, "--urls", str(urls_path), "--cache", str(cache_path), *extra],
                          capture_output=True, timeout=timeout,
                          env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"})
    out = _ANSI.sub("", proc.stdout.decode("utf-8", "replace"))
    return proc.returncode, out, proc.stderr.decode("utf-8", "replace")


def run_urlwatch_report(cfg: Config, report_text: str, infos: list[JobInfo], bot, *, dry_run: bool = False,
                        skip_new: bool = False, baseline: bool = False, use_wayback: bool = False,
                        capture: Callable[[str], tuple[str, str]] | None = None,
                        probe: Callable[[str], str] | None = probe_url,
                        entries: list[ReportEntry] | None = None, state: State | None = None, explain_unreported: bool = False,
                        error_renotify_hours: float = 24, summarizer=None, now: Callable[[], datetime] | None = None,
                        log: Callable[[str], None] = print) -> RunResult:
    """entries を渡すと、report_text を読み直さずそれを使う(再取得で差し替えた結果を渡すため)。
    state を渡すと、同じジョブのエラーは error_renotify_hours 時間は繰り返し通知しない(エラーが直ったら記録を消す)。"""
    result = RunResult()
    by_name = {i.name: i for i in infos}
    if entries is None:
        entries = parse_report(report_text, set(by_name))
    if state is not None and not dry_run:
        for name in by_name:
            if not any(e.name == name and e.status == "ERROR" for e in entries):
                state.clear_error(name)
    # どのジョブがどう報告されたかを必ず出す(「0件」の理由が分かるように)
    counts: dict[str, int] = {}
    for e in entries:
        counts[e.status] = counts.get(e.status, 0) + 1
    log("urlwatch の結果: " + (", ".join(f"{k} {v}件" for k, v in sorted(counts.items())) or "報告なし")
        + f"(ジョブ {len(by_name)}件)")
    unreported = sorted(set(by_name) - {e.name for e in entries})
    unchanged = sorted({e.name for e in entries if e.status == "UNCHANGED"})
    if unchanged:
        log(f"[変更なし] {len(unchanged)}件: " + _safe(", ".join(unchanged), 3000))
    if unreported and not explain_unreported:
        log(f"[報告なし] {len(unreported)}件(変更なし、または無視する一時エラー408/429/5xx)。1件ずつ調べるには --explain-unreported")
    for name in unreported if explain_unreported else []:
        probe_timeout = int(cfg.network.get("probe_timeout_seconds", 20))
        note = (f" / 今のURLの状態: {probe(by_name[name].url, timeout=probe_timeout) if probe is probe_url else probe(by_name[name].url)}"
                if probe else "")
        log(f"[報告なし] {name}: 変更なし、または無視する一時エラー(408/429/5xx)のためレポートに載りませんでした{note}")
    for entry in entries:
        info = by_name.get(entry.name)
        attachment = None
        if entry.status == "UNCHANGED" or not info:
            continue
        if entry.status == "NEW" and skip_new:
            result.skipped += 1
            continue
        if baseline and entry.status in ("NEW", "CHANGED"):   # フィルターを変えた直後など、内容の差を通知せずに記録だけ進める
            log(f"[基準更新] {entry.name}: 変更を通知せず、今の内容を基準にしました")
            result.skipped += 1
            continue
        company = cfg.company(info.company_slug) or {}
        shown_url = info.url if info.is_command else (entry.url or info.url)  # command ジョブは、報告にコマンドが出るので表示用URLを使う
        if entry.status == "ERROR":
            if state is not None and not dry_run:
                if not state.error_due(entry.name, error_renotify_hours):
                    log(f"[エラー通知を省略] {entry.name}: 前回の通知から{error_renotify_hours:g}時間以内です")
                    continue
                state.mark_error(entry.name)
            msg = build_error_message(name=entry.name, url=shown_url, body=entry.body)
            ch = cfg.discord_channels
            if ch.get("errors"):
                targets = [Target(ch["errors"])]
            else:
                targets = _targets(cfg, company, info.roles, None, dry_run)
            messages = [msg]
        else:
            targets = _targets(cfg, company, info.roles, changed_text(entry.body), dry_run)
            archive_url = archive_status = ""
            if use_wayback and not dry_run:
                wb = cfg.network.get("wayback") or {}
                archive_url, archive_status = (capture or (lambda u: wayback_mod.capture(
                    u, timeout=int(wb.get("timeout_seconds", 60)), retries=int(wb.get("retries", 2)),
                    wait=float(wb.get("retry_wait_seconds", 10)))))(shown_url)
                if archive_status:
                    archive_status = f"保存失敗({archive_status})"
            use_ai = summarizer is not None and summarizer.enabled and entry.status == "CHANGED"
            if use_ai:
                summary, note, condensed = summarizer.summarize(info.company_name, entry.name, entry.body)
                stamp = (now() if now else datetime.now(JST)).strftime("%Y%m%d")
                fname = re.sub(r'[\\/:*?"<>|\s]+', "_", f"diff_{stamp}_{info.company_name}") + ".txt"
                attachment = (fname, (f"{entry.name}\nURL: {shown_url}\n\n" + entry.body).encode("utf-8"))
                messages = [build_summary_message(status=entry.status, name=entry.name, url=shown_url,
                                                  archive_url=archive_url, archive_status=archive_status,
                                                  summary=summary, note=note)]
                log(f"[要約] {entry.name}: " + (f"要約しました({len(summary)}字)" if summary
                                                  else f"要約なし({note})"))
            else:
                empty = "(初めて取得しました。以降の変更を通知します)" if entry.status == "NEW" else "(差分の本文なし)"
                chunks = split_long_text(entry.body, CHUNK_LIMIT) or [empty]
                messages = [build_diff_message(status=entry.status, name=entry.name, url=shown_url,
                                               archive_url=archive_url, archive_status=archive_status,
                                               chunk=c, index=n, total=len(chunks))
                            for n, c in enumerate(chunks[:MAX_DIFF_MESSAGES], 1)]
                if len(chunks) > MAX_DIFF_MESSAGES:
                    messages.append(f"(ほか{len(chunks) - MAX_DIFF_MESSAGES}分割は省略しました。URL: {shown_url})")
        if entry.status == "ERROR":
            log(f"[エラー通知] {entry.name} {shown_url}: {_safe(entry.body, 200)}")
        else:
            log(f"[{entry.status}] {entry.name} → {_where(targets)}\n    {shown_url}\n    {_safe(added_text(entry.body), 300)}")
        if not targets:
            result.send_failures.append(f"{entry.name}: 送り先チャンネルがありません(config/discord.yaml を確認)")
            continue
        for t in targets:
            for m in messages:
                try:
                    if attachment:
                        bot.send_message_with_file(t.channel_id, m, attachment[0], attachment[1], suppress_embeds=True)
                    else:
                        bot.send_message(t.channel_id, m, suppress_embeds=True)
                    result.sent += 1
                except DiscordError as e:
                    result.send_failures.append(f"{entry.name} → {_mask(t.channel_id)}: {e}")
                    break  # このチャンネルの残りは送らない(順序が崩れるため)
    if summarizer is not None and summarizer.enabled:
        log(f"[要約] AI要約: {summarizer.used}件(上限{summarizer.max_per_run}件)、失敗{summarizer.failed}件")
    if state is not None and not dry_run:
        state.save()
    return result
