"""設定ファイル(common.yaml / config/discord.yaml / companies/*.yaml)の読み込みと共通ヘルパー。"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

# scripts/watchlib/config.py → リポジトリのルート
ROOT = Path(__file__).resolve().parents[2]


class ConfigError(Exception):
    """設定ファイルを読めない、など。利用者が直すべき問題。"""


def load_yaml(path: Path) -> Any:
    try:
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f)
    except FileNotFoundError as e:
        raise ConfigError(f"ファイルが見つかりません: {path}") from e
    except yaml.YAMLError as e:
        raise ConfigError(f"YAMLとして読めません: {path}: {e}") from e


def normalize_handle(handle: Any) -> str | None:
    """アカウントIDから @ を取り除く。空や null は None。"""
    if handle is None:
        return None
    text = str(handle).strip().lstrip("@").strip()
    return text or None


def apply_adjust(base: list[str], adj: dict | None) -> list[str]:
    """共通のリストに、会社ごとの add / remove を反映する(順序は保つ)。"""
    adj = adj or {}
    remove = set(adj.get("remove") or [])
    out = [w for w in base if w not in remove]
    for w in adj.get("add") or []:
        if w not in out:
            out.append(w)
    return out


def channel_id(value: Any) -> str | None:
    """チャンネルIDは桁が多いので、文字列として扱う。"""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


@dataclass
class Config:
    root: Path
    common: dict
    discord: dict
    companies: list[dict]

    @property
    def job_roles(self) -> dict:
        return self.common.get("job_roles") or {}

    @property
    def sns(self) -> dict:
        return self.common.get("sns") or {}

    @property
    def web(self) -> dict:
        return self.common.get("web") or {}

    @property
    def discord_channels(self) -> dict:
        """config/discord.yaml を、チャンネルIDを文字列にそろえて返す。"""
        d = (self.discord or {}).get("discord") or {}
        ch = d.get("channels") or {}
        return {
            "api_base": d.get("api_base") or "https://discord.com/api/v10",
            "fallback": channel_id(ch.get("fallback")),
            "errors": channel_id(ch.get("errors")),
            "roles": {k: channel_id(v) for k, v in (ch.get("roles") or {}).items()},
        }

    def company(self, slug: str) -> dict | None:
        return next((c for c in self.companies if c.get("slug") == slug), None)


def load_config(
    root: Path | None = None,
    companies_dir: Path | None = None,
    common_path: Path | None = None,
    discord_path: Path | None = None,
    require_discord: bool = False,
) -> Config:
    root = Path(root) if root else ROOT
    common_path = Path(common_path) if common_path else root / "common.yaml"
    companies_dir = Path(companies_dir) if companies_dir else root / "companies"
    discord_path = Path(discord_path) if discord_path else root / "config" / "discord.yaml"

    common = load_yaml(common_path)
    if not isinstance(common, dict):
        raise ConfigError(f"{common_path}: 最上位が辞書ではありません")

    if discord_path.exists():
        discord = load_yaml(discord_path) or {}
    elif require_discord:
        raise ConfigError(f"ファイルが見つかりません: {discord_path}")
    else:
        discord = {}

    companies: list[dict] = []
    if companies_dir.exists():
        for p in sorted(companies_dir.glob("*.yaml")):
            data = load_yaml(p)
            if not isinstance(data, dict):
                raise ConfigError(f"{p}: 最上位が辞書ではありません")
            data["_file"] = p.name
            companies.append(data)
    return Config(root=root, common=common, discord=discord, companies=companies)


# --- 会社ファイルの読み取りヘルパー ---------------------------------------------------

def company_role_entries(company: dict) -> list[dict]:
    """roles を [{"id": ..., "exclude_words": {add, remove}}] にそろえる(文字列形式も辞書形式も可)。"""
    entries: list[dict] = []
    for item in company.get("roles") or []:
        if isinstance(item, str):
            entries.append({"id": item, "exclude_words": {}})
        elif isinstance(item, dict) and isinstance(item.get("id"), str):
            entries.append({"id": item["id"], "exclude_words": item.get("exclude_words") or {}})
    return entries


def company_role_ids(company: dict) -> list[str]:
    return [e["id"] for e in company_role_entries(company)]


def role_exclude_words(job_roles: dict, entry: dict) -> list[str]:
    """職種の除外語に、会社ごとの add / remove を反映したもの。"""
    base = (job_roles.get(entry["id"]) or {}).get("exclude_words") or []
    return apply_adjust(base, entry.get("exclude_words"))
