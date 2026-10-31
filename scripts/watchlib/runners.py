"""RSSHub通知・urlwatch通知の本体(CLIから呼ぶ。テストしやすいよう、副作用は引数で受け取る)。"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .config import Config
from .discord import DiscordError
from .messages import (build_diff_message, build_error_message, build_feed_error_message, build_sns_message,
                       split_long_text, CHUNK_LIMIT)
from .report import ReportEntry, parse_report
from .routing import Target, pick_roles, resolve_channels
from .rsshub import Feed, FeedFetchError, fetch_feed
from .state import State
from .urlwatch_jobs import JobInfo
from . import wayback as wayback_mod

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


def _targets(cfg: Config, company: dict, hint_roles, text, dry_run: bool) -> list[Target]:
    roles = pick_roles(cfg, company, hint_roles=hint_roles, text=text)
    targets = resolve_channels(roles, cfg.discord_channels)
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

def run_rsshub(cfg: Config, feeds: list[Feed], state: State, bot, *, dry_run: bool = False,
               notify_on_first_run: bool = False, limit: int = 5,
               fetch: Callable[[str], list] = fetch_feed, build_errors: list[tuple[str, str]] | None = None,
               log: Callable[[str], None] = print) -> RunResult:
    result = RunResult(errors=list(build_errors or []))
    for feed in feeds:
        company = cfg.company(feed.company_slug) or {}
        try:
            items = fetch(feed.request_url)
        except FeedFetchError as e:
            result.errors.append((feed.key, str(e)))
            log(f"[取得失敗] {feed.key}: {e}")
            continue
        first_run = not state.initialized(feed.key)
        if first_run and not notify_on_first_run:
            if not dry_run:
                state.mark(feed.key, [i.id for i in items])
                state.save()
            log(f"[初回] {feed.key}: {len(items)}件を既読にしました(通知なし)")
            continue
        new = [i for i in items if not state.seen(feed.key, i.id)]
        if not new:
            continue
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
    if result.errors:
        send_to_errors_channel(cfg, bot, build_feed_error_message(result.errors), result, dry_run=dry_run)
    return result


# --- urlwatch --------------------------------------------------------------------------------

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def run_urlwatch_command(urls_path: Path, cache_path: Path, command: str = "urlwatch",
                         timeout: int = 1800) -> tuple[int, str, str]:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run([command, "--urls", str(urls_path), "--cache", str(cache_path)],
                          capture_output=True, timeout=timeout,
                          env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"})
    out = _ANSI.sub("", proc.stdout.decode("utf-8", "replace"))
    return proc.returncode, out, proc.stderr.decode("utf-8", "replace")


def run_urlwatch_report(cfg: Config, report_text: str, infos: list[JobInfo], bot, *, dry_run: bool = False,
                        skip_new: bool = False, use_wayback: bool = False,
                        capture: Callable[[str], tuple[str, str]] | None = None,
                        log: Callable[[str], None] = print) -> RunResult:
    result = RunResult()
    by_name = {i.name: i for i in infos}
    entries = parse_report(report_text, set(by_name))
    for entry in entries:
        info = by_name.get(entry.name)
        if entry.status == "UNCHANGED" or not info:
            continue
        if entry.status == "NEW" and skip_new:
            result.skipped += 1
            continue
        company = cfg.company(info.company_slug) or {}
        if entry.status == "ERROR":
            msg = build_error_message(name=entry.name, url=entry.url or info.url, body=entry.body)
            ch = cfg.discord_channels
            if ch.get("errors"):
                targets = [Target(ch["errors"])]
            else:
                targets = _targets(cfg, company, info.roles, None, dry_run)
            messages = [msg]
        else:
            targets = _targets(cfg, company, info.roles, None, dry_run)
            archive_url = archive_status = ""
            if use_wayback and not dry_run:
                archive_url, archive_status = (capture or wayback_mod.capture)(entry.url or info.url)
                if archive_status:
                    archive_status = f"保存失敗({archive_status})"
            empty = "(初めて取得しました。以降の変更を通知します)" if entry.status == "NEW" else "(差分の本文なし)"
            chunks = split_long_text(entry.body, CHUNK_LIMIT) or [empty]
            messages = [build_diff_message(status=entry.status, name=entry.name, url=entry.url or info.url,
                                           archive_url=archive_url, archive_status=archive_status,
                                           chunk=c, index=n, total=len(chunks))
                        for n, c in enumerate(chunks[:MAX_DIFF_MESSAGES], 1)]
            if len(chunks) > MAX_DIFF_MESSAGES:
                messages.append(f"(ほか{len(chunks) - MAX_DIFF_MESSAGES}分割は省略しました。URL: {entry.url or info.url})")
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
    return result
