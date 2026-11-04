"""RSSHub通知・urlwatch通知の本体(CLIから呼ぶ。テストしやすいよう、副作用は引数で受け取る)。"""
from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .config import Config
from .discord import DiscordError
from .messages import (build_diff_message, build_error_message, build_feed_error_message, build_sns_message,
                       split_long_text, CHUNK_LIMIT)
from .report import ReportEntry, parse_report
from .routing import Target, pick_roles, resolve_channels
from .rsshub import Feed, FeedFetchError, fetch_feed, is_retweet
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
        for t in targets:
            try:
                bot.send_message(t.channel_id, text, suppress_embeds=False)
                result.sent += 1
            except DiscordError as e:
                ok = False
                result.send_failures.append(f"{feed.key} → {t.channel_id}: {e}")
        if ok and not dry_run:
            state.mark(feed.key, [item.id])  # 送れなかったものは既読にせず、次回また試す
    if not dry_run:
        state.save()


def run_rsshub(cfg: Config, feeds: list[Feed], state: State, bot, *, dry_run: bool = False,
               notify_on_first_run: bool = False, limit: int = 5,
               fetch: Callable[[str], list] | None = None, build_errors: list[tuple[str, str]] | None = None,
               retry_rounds: int | None = None, retry_wait: float | None = None,
               request_interval: float | None = None, error_renotify_hours: float | None = None,
               sleep: Callable[[float], None] = time.sleep, log: Callable[[str], None] = print) -> RunResult:
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
    if fetch is None:
        fetch = lambda url: fetch_feed(url, timeout=timeout)  # noqa: E731

    result = RunResult(errors=[])
    last_error: dict[str, str] = {}
    pending = list(feeds)
    for attempt in range(rounds + 1):
        if attempt:
            if not pending:
                break
            log(f"[再試行 {attempt}/{rounds}] 取得に失敗した{len(pending)}件を、{wait:g}秒待ってから取得し直します")
            sleep(wait)
        failed: list[Feed] = []
        for n, feed in enumerate(pending):
            if n and interval > 0:
                sleep(interval)  # 続けざまに要求しない
            try:
                items = fetch(feed.request_url)
            except FeedFetchError as e:
                last_error[feed.key] = str(e)
                failed.append(feed)
                log(f"[取得失敗] {feed.key}: {e}" + (f"(試行{attempt + 1}回目)" if rounds else ""))
                continue
            if not dry_run:
                state.clear_error(feed.key)
            _process_feed(cfg, feed, items, state, bot, result, cfg.company(feed.company_slug) or {},
                          dry_run=dry_run, notify_on_first_run=notify_on_first_run, limit=limit, log=log)
        pending = failed

    unresolved = [(f.key, last_error[f.key]) for f in pending] + list(build_errors or [])
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
                        skip_new: bool = False, use_wayback: bool = False,
                        capture: Callable[[str], tuple[str, str]] | None = None,
                        probe: Callable[[str], str] | None = probe_url,
                        entries: list[ReportEntry] | None = None, state: State | None = None,
                        error_renotify_hours: float = 24,
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
    for name in sorted(set(by_name) - {e.name for e in entries}):
        probe_timeout = int(cfg.network.get("probe_timeout_seconds", 20))
        note = (f" / 今のURLの状態: {probe(by_name[name].url, timeout=probe_timeout) if probe is probe_url else probe(by_name[name].url)}"
                if probe else "")
        log(f"[報告なし] {name}: 変更なし、または無視する一時エラー(408/429/5xx)のためレポートに載りませんでした。フィルターの失敗は max_tries 回続くまで報告されません{note}")
    for entry in entries:
        info = by_name.get(entry.name)
        if entry.status == "UNCHANGED" or not info:
            continue
        if entry.status == "NEW" and skip_new:
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
            targets = _targets(cfg, company, info.roles, added_text(entry.body), dry_run)
            archive_url = archive_status = ""
            if use_wayback and not dry_run:
                wb = cfg.network.get("wayback") or {}
                archive_url, archive_status = (capture or (lambda u: wayback_mod.capture(
                    u, timeout=int(wb.get("timeout_seconds", 60)), retries=int(wb.get("retries", 2)),
                    wait=float(wb.get("retry_wait_seconds", 10)))))(shown_url)
                if archive_status:
                    archive_status = f"保存失敗({archive_status})"
            empty = "(初めて取得しました。以降の変更を通知します)" if entry.status == "NEW" else "(差分の本文なし)"
            chunks = split_long_text(entry.body, CHUNK_LIMIT) or [empty]
            messages = [build_diff_message(status=entry.status, name=entry.name, url=shown_url,
                                           archive_url=archive_url, archive_status=archive_status,
                                           chunk=c, index=n, total=len(chunks))
                        for n, c in enumerate(chunks[:MAX_DIFF_MESSAGES], 1)]
            if len(chunks) > MAX_DIFF_MESSAGES:
                messages.append(f"(ほか{len(chunks) - MAX_DIFF_MESSAGES}分割は省略しました。URL: {shown_url})")
        if not targets:
            result.send_failures.append(f"{entry.name}: 送り先チャンネルがありません(config/discord.yaml を確認)")
            continue
        for t in targets:
            for m in messages:
                try:
                    bot.send_message(t.channel_id, m, suppress_embeds=True)
                    result.sent += 1
                except DiscordError as e:
                    result.send_failures.append(f"{entry.name} → {t.channel_id}: {e}")
                    break  # このチャンネルの残りは送らない(順序が崩れるため)
    if state is not None and not dry_run:
        state.save()
    return result
