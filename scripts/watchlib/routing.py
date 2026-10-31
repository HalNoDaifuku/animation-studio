"""通知の送り先(職種ごとのDiscordチャンネル)を決める。

ルール:
  1. 候補の職種 = 監視URLの roles(空なら、その会社の全職種)
  2. SNSの投稿は、本文に職種のキーワードがあれば、その職種だけに絞る
     (職種の除外語を含む投稿は、その職種には送らない。絞れなければ候補の全職種)
  3. 職種 → チャンネルは config/discord.yaml の channels.roles。未設定の職種があれば fallback へ
  4. 同じチャンネルに複数の職種が割り当てられていても、1回しか送らない
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .config import Config, company_role_entries, role_exclude_words

_ASCII_WORD = re.compile(r"[A-Za-z0-9 ._+\-]+")


def keyword_in_text(keyword: str, text: str) -> bool:
    """日本語は部分一致。英数字だけの語(TA, VFX, 3DCG など)は、前後が英数字でないときだけ一致。

    「TA」が「data」や「STAFF」に一致しないようにするため。
    """
    if not keyword:
        return False
    if _ASCII_WORD.fullmatch(keyword):
        pattern = r"(?<![A-Za-z0-9])" + re.escape(keyword) + r"(?![A-Za-z0-9])"
        return re.search(pattern, text, re.IGNORECASE) is not None
    return keyword in text


def match_roles(text: str, entries: list[dict], job_roles: dict) -> list[str]:
    """本文に、職種のキーワードがあり、その職種の除外語がないもの。"""
    matched: list[str] = []
    for entry in entries:
        role = job_roles.get(entry["id"])
        if not role:
            continue
        if not any(keyword_in_text(k, text) for k in role.get("keywords") or []):
            continue
        if any(keyword_in_text(w, text) for w in role_exclude_words(job_roles, entry)):
            continue
        matched.append(entry["id"])
    return matched


def pick_roles(
    cfg: Config,
    company: dict,
    *,
    hint_roles: list[str] | None = None,
    text: str | None = None,
) -> list[str]:
    """通知の対象になる職種IDのリスト。"""
    entries = company_role_entries(company)
    if hint_roles:
        entries = [e for e in entries if e["id"] in hint_roles]
    if text:
        matched = match_roles(text, entries, cfg.job_roles)
        if matched:
            return matched
    return [e["id"] for e in entries]


@dataclass
class Target:
    channel_id: str
    roles: list[str] = field(default_factory=list)
    is_fallback: bool = False


def resolve_channels(role_ids: list[str], discord_channels: dict) -> list[Target]:
    """職種IDのリストを、送り先チャンネルにする(重複なし)。"""
    role_map = discord_channels.get("roles") or {}
    fallback = discord_channels.get("fallback")
    targets: dict[str, Target] = {}
    unmapped: list[str] = []
    for rid in role_ids:
        cid = role_map.get(rid)
        if cid:
            targets.setdefault(cid, Target(cid)).roles.append(rid)
        else:
            unmapped.append(rid)
    if (unmapped or not role_ids) and fallback:
        t = targets.setdefault(fallback, Target(fallback, is_fallback=True))
        t.roles.extend(unmapped)
    return list(targets.values())
