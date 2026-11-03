"""Discord サーバーに、職種ごとのチャンネルを作る(discord_setup_channels.py 用)。

カテゴリ1つに置けるチャンネルは最大50。超えるときは「(2)」「(3)」のカテゴリを作る。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .config import Config, company_role_ids

GUILD_TEXT, GUILD_CATEGORY = 0, 4
MAX_PER_CATEGORY = 50
FALLBACK_NAME, ERRORS_NAME = "未分類", "監視エラー"


@dataclass
class Planned:
    key: str            # 職種ID、または "fallback" / "errors"
    name: str           # チャンネル名
    topic: str
    category: str       # カテゴリ名
    existing_id: str | None = None


def channel_name(text: str) -> str:
    """Discord のチャンネル名にそろえる(小文字・空白とアンダースコアはハイフン)。"""
    name = re.sub(r"[\s_]+", "-", text.strip().lower())
    return re.sub(r"-{2,}", "-", name).strip("-")[:100] or "channel"


def used_role_ids(cfg: Config) -> list[str]:
    if any(co.get("type", "studio") == "studio" for co in cfg.companies):
        return list(cfg.job_roles)  # 通知は本文から全職種に振り分けるので、全職種ぶんのチャンネルが要る(common.yaml の並び順)
    used = {r for co in cfg.companies for r in company_role_ids(co)}
    return [r for r in cfg.job_roles if r in used]


def used_types(cfg: Config) -> list[str]:
    """studio 以外で、会社ファイルに使われている種別(common.yaml の並び順)。"""
    used = {co.get("type") for co in cfg.companies if co.get("type") and co.get("type") != "studio"}
    return [t for t in (cfg.common.get("company_types") or {}) if t in used]


def plan_channels(cfg: Config, existing: list[dict], category_name: str, role_ids: list[str]) -> list[Planned]:
    """作る(または再利用する)チャンネルの計画。名前が同じ既存チャンネルは再利用する。"""
    by_name = {c["name"]: c for c in existing if c.get("type") == GUILD_TEXT}
    items: list[tuple[str, str, str]] = [("fallback", FALLBACK_NAME, "どの職種にも当てはまらない通知"),
                                         ("errors", ERRORS_NAME, "サイト監視・RSSHubの取得エラー")]
    # アニメ会社以外の種別(portal など)の会社があれば、その種別のチャンネルも作る
    for type_id in used_types(cfg):
        label = (cfg.common.get("company_types") or {}).get(type_id, {}).get("name", type_id)
        items.append((f"type:{type_id}", channel_name(type_id), f"{label}の通知"))
    items += [(rid, channel_name(rid), cfg.job_roles[rid].get("name", rid)) for rid in role_ids]
    plan: list[Planned] = []
    for n, (key, name, topic) in enumerate(items):
        page = n // MAX_PER_CATEGORY
        cat = category_name if page == 0 else f"{category_name} ({page + 1})"
        cname = channel_name(name)
        found = by_name.get(cname)
        plan.append(Planned(key, cname, topic, cat, str(found["id"]) if found else None))
    return plan


def apply_plan(bot, guild_id: str, plan: list[Planned], existing: list[dict]) -> dict[str, str]:
    """計画どおりに作成し、{key: チャンネルID} を返す。"""
    categories = {c["name"]: str(c["id"]) for c in existing if c.get("type") == GUILD_CATEGORY}
    result: dict[str, str] = {}
    for p in plan:
        if p.existing_id:
            result[p.key] = p.existing_id
            continue
        if p.category not in categories:
            created = bot.create_guild_channel(guild_id, {"name": p.category, "type": GUILD_CATEGORY})
            categories[p.category] = str(created["id"])
        created = bot.create_guild_channel(guild_id, {"name": p.name, "type": GUILD_TEXT, "topic": p.topic,
                                                      "parent_id": categories[p.category]})
        result[p.key] = str(created["id"])
    return result


def mapping_yaml(cfg: Config, plan: list[Planned], ids: dict[str, str | None]) -> str:
    def val(key: str) -> str:
        v = ids.get(key)
        return f'"{v}"' if v else "null   # (作成予定)"
    lines = ["discord:", "  channels:", f"    fallback: {val('fallback')}", f"    errors: {val('errors')}"]
    types = [p for p in plan if p.key.startswith("type:")]
    if types:
        lines.append("    types:")
        lines += [f"      {p.key[5:]}: {val(p.key)}   # {p.topic}" for p in types]
    lines.append("    roles:")
    for p in plan:
        if p.key in ("fallback", "errors") or p.key.startswith("type:"):
            continue
        lines.append(f"      {p.key}: {val(p.key)}   # {p.topic}")
    return "\n".join(lines)
