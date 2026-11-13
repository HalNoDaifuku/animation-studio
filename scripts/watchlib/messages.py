"""Discordに送るメッセージの組み立て(2000文字の上限に必ず収める)。"""
from __future__ import annotations

import datetime as dt
import re

DISCORD_LIMIT = 2000
CHUNK_LIMIT = 1450  # 差分の本文を1メッセージに入れる最大文字数(見出しぶんの余裕を残す)

STATUS_LABEL = {
    "NEW": "新規検知",
    "CHANGED": "変更検知",
    "ERROR": "エラー",
    "UNCHANGED": "変更なし",
}

JST = dt.timezone(dt.timedelta(hours=9))


def truncate(text: str, limit: int = DISCORD_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 4] + "\n..."


def split_long_text(text: str, limit: int = CHUNK_LIMIT) -> list[str]:
    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        if len(current) + len(line) > limit:
            if current.strip():
                chunks.append(current.rstrip())
                current = ""
            while len(line) > limit:
                chunks.append(line[:limit].rstrip())
                line = line[limit:]
        current += line
    if current.strip():
        chunks.append(current.rstrip())
    return chunks


def build_diff_message(
    *,
    status: str,
    name: str,
    url: str,
    archive_url: str,
    archive_status: str,
    chunk: str,
    index: int,
    total: int,
) -> str:
    """urlwatch の差分1件(の1分割ぶん)を、2000文字以内のメッセージにする。"""
    label = STATUS_LABEL.get(status, status)
    title = f"**{label}: {name}**"
    url_line = f"\nURL: {url}" if url else ""
    if archive_url:
        archive_line = f"\nWayback: {archive_url}"
    elif archive_status:
        archive_line = f"\nWayback: {archive_status}"
    else:
        archive_line = ""
    chunk_line = f"\n分割: {index}/{total}" if total > 1 else ""
    # コードブロックを壊さないよう、本文中の ``` は無害化する
    chunk = chunk.replace("```", "'''")

    def assemble(body: str) -> str:
        return f"{title}{url_line}{archive_line}{chunk_line}\n```diff\n{body}\n```"

    message = assemble(chunk)
    if len(message) > DISCORD_LIMIT:
        over = len(message) - DISCORD_LIMIT
        message = assemble(chunk[: max(0, len(chunk) - over - 20)] + "\n...(省略)")
    if len(message) > DISCORD_LIMIT:
        message = assemble("(内容が長すぎるため省略しました)")
    return truncate(message)


def build_summary_message(*, status: str, name: str, url: str, archive_url: str, archive_status: str,
                          summary: str | None, note: str, added_preview: str = "") -> str:
    """AI要約つきの変更通知(差分本体は、ファイルとして添付する)。
    summary があれば要約を、なければ理由(note)を書く。note が "short" のときは、差分が短いので追加された内容をそのまま載せる。"""
    label = STATUS_LABEL.get(status, status)
    lines = [f"**{label}: {name}**"]
    if url:
        lines.append(f"URL: {url}")
    if summary:
        lines.append("🤖 AI要約(誤りがあり得ます。原文は添付の差分を確認してください)")
        lines.append(truncate(summary, 1200))
    elif note == "short":
        shown = (added_preview or "(追加された行はありません)").replace("```", "'''")
        lines.append("追加された内容(短いため、要約せずにそのまま載せます):")
        lines.append("```\n" + truncate(shown, 800) + "\n```")
    else:
        lines.append(f"(要約なし: {note})")
    if archive_url:
        lines.append(f"Wayback: {archive_url}")
    elif archive_status:
        lines.append(f"Wayback: {archive_status}")
    return truncate("\n".join(lines))


def build_error_message(*, name: str, url: str, body: str) -> str:
    head = f"**エラー: {name}**"
    if url:
        head += f"\nURL: {url}"
    body = body.replace("```", "'''")
    return truncate(f"{head}\n```\n{truncate(body, 1500)}\n```")


def format_jst(value: str | None) -> str:
    """RSSHub の日時(ISO 8601)を、日本時間の「2026-10-29 21:45」にする。読めなければそのまま。"""
    if not value:
        return ""
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(JST).strftime("%Y-%m-%d %H:%M")


def build_sns_message(
    *,
    company_name: str,
    platform_label: str,
    account_label: str,
    text: str,
    url: str,
    published: str | None,
) -> str:
    where = platform_label + (f" / {account_label}" if account_label else "")
    lines = [f"**SNS: {company_name} ({where})**"]
    when = format_jst(published)
    if when:
        lines.append(when)
    body = re.sub(r"\n{3,}", "\n\n", text.strip())
    if len(body) > 500:
        body = body[:500] + "…"
    if body:
        lines.append(body)
    if url:
        lines.append(url)
    return truncate("\n".join(lines))


def build_feed_error_message(errors: list[tuple[str, str]], max_items: int = 10) -> str:
    lines = [f"**RSSHub取得エラー: {len(errors)}件**"]
    for label, reason in errors[:max_items]:
        lines.append(f"- {label}: {reason[:150]}")
    if len(errors) > max_items:
        lines.append(f"- ほか{len(errors) - max_items}件")
    return truncate("\n".join(lines))
