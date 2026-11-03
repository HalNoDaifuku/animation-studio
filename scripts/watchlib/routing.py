"""通知の送り先(職種ごとのDiscordチャンネル)を決める。

ルール(職種ごとに分けて送る):
  1. 通知の本文(SNSの投稿、サイトの差分で増えた行)に、職種のキーワードがある職種だけに送る。
     職種の除外語を含む職種には送らない。
  2. 本文から職種が分からない通知は、全職種には送らず、種別のチャンネル → fallback(未分類)へ送る。
     ただし、監視URLに roles を書いた(職種専用のページ)場合は、その職種へ送る。
  3. 職種の候補は、監視URLの roles(空なら common.yaml の全職種)。会社の roles は、その会社だけ
     除外語を調整したいときの上書きに使う。就活サイトなど(portal)は、会社の roles を書いたときだけ職種で分け、
     書いていなければ、種別のチャンネルへ送る。
  4. 職種 → チャンネルは config/discord.yaml の channels.roles。未設定の職種があれば fallback へ
  5. 同じチャンネルに複数の職種が割り当てられていても、1回しか送らない
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
    own = {e["id"]: e for e in company_role_entries(company)}   # 会社ごとの除外語の調整つきの職種
    if company.get("type", "studio") == "studio":
        entries = [own.get(rid) or {"id": rid, "exclude_words": {}} for rid in cfg.job_roles]
        for rid, e in own.items():                               # 辞書にない職種IDも残す(検査でエラーになる)
            if rid not in cfg.job_roles:
                entries.append(e)
    else:
        entries = list(own.values())                             # 就活サイトなど(portal)は、roles を書いたときだけ職種で分ける
    if hint_roles:
        entries = [e for e in entries if e["id"] in hint_roles]
    if text:
        matched = match_roles(text, entries, cfg.job_roles)
        if matched:
            return matched
    # 職種が分からない通知: 職種専用のページ(roles 指定あり)ならその職種、そうでなければ職種なし(→ fallback)
    return [e["id"] for e in entries] if hint_roles else []


@dataclass
class Target:
    channel_id: str
    roles: list[str] = field(default_factory=list)
    is_fallback: bool = False


def resolve_channels(role_ids: list[str], discord_channels: dict, company_type: str | None = None) -> list[Target]:
    """職種IDのリストを、送り先チャンネルにする(重複なし)。

    職種が無い・職種にチャンネルが無い通知は、会社の type のチャンネル → fallback の順で送る。
    """
    role_map = discord_channels.get("roles") or {}
    fallback = (discord_channels.get("types") or {}).get(company_type or "studio") or discord_channels.get("fallback")
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
