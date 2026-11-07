"""設定ファイルの検査。エラー(動かない・誤動作する)と警告(動くが怪しい)を返す。"""
from __future__ import annotations

import datetime as dt
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

from .config import Config, ConfigError, load_config, normalize_handle
from .rsshub import KNOWN_PLACEHOLDERS, company_feeds
from .urlwatch_jobs import job_name

ERROR, WARNING = "error", "warning"

KNOWN_FILTERS = {
    "beautify", "html2text", "strip", "striplines", "css", "xpath", "re.sub", "re.findall", "grep", "grepi",
    "keep_lines_containing", "delete_lines_containing", "sort", "reverse", "format-json", "jq", "pdf2text",
    "ocr", "ical2text", "sha1sum", "hexdump", "remove-duplicate-lines", "remove_repeated", "element-by-class",
    "element-by-id", "element-by-style", "element-by-tag", "csv2text", "shellpipe", "execute", "wdiff",
}
COMPANY_KEYS = {
    "schema_version", "type", "id", "slug", "name", "display_name", "name_en", "name_history", "founded_year",
    "location", "roles", "related_companies", "website", "sns", "monitors", "_file", "_folder",
}
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]*$")
ROLE_ID = re.compile(r"^[a-z][a-z0-9_]*$")
HANDLE = re.compile(r"^[A-Za-z0-9_.\-]+$")
CHANNEL_ID = re.compile(r"^\d{17,20}$")


@dataclass
class Issue:
    level: str
    where: str      # ファイル名(+ 項目の場所)
    message: str

    def as_dict(self) -> dict:
        return {"level": self.level, "where": self.where, "message": self.message}


class _Collector:
    def __init__(self) -> None:
        self.issues: list[Issue] = []

    def error(self, where: str, msg: str) -> None:
        self.issues.append(Issue(ERROR, where, msg))

    def warn(self, where: str, msg: str) -> None:
        self.issues.append(Issue(WARNING, where, msg))


def _is_str_list(v) -> bool:
    return isinstance(v, list) and all(isinstance(x, str) and x.strip() for x in v)


def _compile(c: _Collector, where: str, pattern) -> None:
    if not isinstance(pattern, str):
        c.error(where, f"正規表現は文字列で書いてください: {pattern!r}")
        return
    try:
        re.compile(pattern)
    except re.error as e:
        c.error(where, f"正規表現が不正です: {e}(パターン: {pattern[:60]})")


def check_filter_list(c: _Collector, where: str, filters) -> None:
    if not isinstance(filters, list):
        c.error(where, "filter はリストで書いてください")
        return
    for i, f in enumerate(filters):
        w = f"{where}[{i}]"
        if isinstance(f, str):
            name = f.split(":", 1)[0]
            if name not in KNOWN_FILTERS:
                c.warn(w, f"知らないフィルター名です: {name}(プラグインなら無視してください)")
        elif isinstance(f, dict) and len(f) == 1:
            name, arg = next(iter(f.items()))
            if name not in KNOWN_FILTERS:
                c.warn(w, f"知らないフィルター名です: {name}(プラグインなら無視してください)")
            if name in ("re.sub", "re.findall"):
                if isinstance(arg, dict):
                    if "pattern" not in arg:
                        c.error(w, f"{name} には pattern が必要です")
                    else:
                        _compile(c, w, arg["pattern"])
                    if name == "re.sub" and "repl" in arg and not isinstance(arg["repl"], str):
                        c.error(w, "repl は文字列で書いてください")
                elif isinstance(arg, str):
                    _compile(c, w, arg.split(",", 1)[0] if name == "re.sub" else arg)
                else:
                    c.error(w, f"{name} の指定が不正です")
        else:
            c.error(w, "フィルターは「名前」または「名前: 引数」の1つだけの形で書いてください")


# --- common.yaml ------------------------------------------------------------------------------

def check_common(c: _Collector, cfg: Config) -> None:
    f = "config/common.yaml"
    if (cfg.root / "common.yaml").exists():
        c.warn("common.yaml", "古い場所の common.yaml が残っています。設定は config/common.yaml に移したので、リポジトリ直下の common.yaml は削除してください")
    roles = cfg.job_roles
    if not isinstance(roles, dict) or not roles:
        c.error(f"{f}: job_roles", "職種が1つもありません")
        roles = {}
    names: dict[str, str] = {}
    for rid, role in roles.items():
        w = f"{f}: job_roles.{rid}"
        if not ROLE_ID.match(str(rid)):
            c.error(w, "職種IDは小文字の snake_case にしてください")
        if not isinstance(role, dict):
            c.error(w, "職種の中身が辞書ではありません")
            continue
        name = role.get("name")
        if not isinstance(name, str) or not name.strip():
            c.error(w, "name(職種名)が必要です")
        elif name in names:
            c.error(w, f"職種名が {names[name]} と重複しています: {name}")
        else:
            names[name] = str(rid)
        kws, exs = role.get("keywords"), role.get("exclude_words", [])
        if not _is_str_list(kws) or not kws:
            c.error(w, "keywords は空でない文字列のリストにしてください")
            kws = []
        if not _is_str_list(exs):
            c.error(w, "exclude_words は文字列のリストにしてください(空は [])")
            exs = []
        for label, words in (("keywords", kws), ("exclude_words", exs)):
            if len(set(words)) != len(words):
                c.warn(w, f"{label} に重複があります")
            for word in words:
                if '"' in word:
                    c.error(w, f'{label} に " は使えません(検索クエリが壊れます): {word}')
        for word in exs:
            hit = next((k for k in kws if word in k), None)
            if hit:
                c.error(w, f"除外語「{word}」は自分のキーワード「{hit}」に含まれるため、その職種を自分で消してしまいます")

    types = cfg.common.get("company_types")
    if not isinstance(types, dict) or "studio" not in types:
        c.error(f"{f}: company_types", "company_types に studio が必要です")
    sns = cfg.sns
    platforms = sns.get("platforms")
    if not isinstance(platforms, dict) or not platforms:
        c.error(f"{f}: sns.platforms", "プラットフォームが1つもありません")
        platforms = {}
    for pid, p in platforms.items():
        w = f"{f}: sns.platforms.{pid}"
        if not isinstance(p, dict):
            c.error(w, "辞書ではありません")
            continue
        uf = p.get("user_feed")
        if not isinstance(uf, str) or "{base}" not in uf or "{handle}" not in uf:
            c.error(w, "user_feed には {base} と {handle} が必要です")
        sf = p.get("search_feed")
        if sf is not None and (not isinstance(sf, str) or "{base}" not in sf or "{query}" not in sf):
            c.error(w, "search_feed は null か、{base} と {query} を含む文字列にしてください")
        up = p.get("user_params")
        if up is not None and (not isinstance(up, str) or up.startswith(("?", "&"))):
            c.error(w, "user_params は「a=1&b=2」の形の文字列にしてください(先頭に ? や & は付けない)")
        if not isinstance(p.get("label"), str):
            c.warn(w, "label(表示名)がありません")

    df = sns.get("default_filter") or {}
    inc, exc = df.get("include"), df.get("exclude")
    if not _is_str_list(inc) or not inc:
        c.error(f"{f}: sns.default_filter.include", "含める語は空でない文字列のリストにしてください")
        inc = []
    if not _is_str_list(exc):
        c.error(f"{f}: sns.default_filter.exclude", "弾く語は文字列のリストにしてください(空は [])")
        exc = []
    for w in set(inc) & set(exc):
        c.error(f"{f}: sns.default_filter", f"「{w}」が include と exclude の両方にあります(全部弾かれます)")
    tpl = df.get("rsshub_query", "")
    if "{include}" not in str(tpl):
        c.error(f"{f}: sns.default_filter.rsshub_query", "{include} が必要です")

    if not isinstance(sns.get("exclude_retweets", True), bool):
        c.error(f"{f}: sns.exclude_retweets", "true / false にしてください")
    limit = sns.get("search_max_query_length")
    if not isinstance(limit, int) or not 100 <= limit <= 1000:
        c.error(f"{f}: sns.search_max_query_length", "100〜1000 の整数にしてください")
    for name, spec in (sns.get("search_presets") or {}).items():
        w = f"{f}: sns.search_presets.{name}"
        if not isinstance(spec, dict):
            c.error(w, "辞書ではありません")
            continue
        for platform, body in spec.items():
            if platform == "description":
                continue
            if platform not in platforms:
                c.error(w, f"未定義のプラットフォーム: {platform}")
                continue
            if not (platforms[platform] or {}).get("search_feed"):
                c.warn(w, f"{platform} には search_feed が無いので、このプリセットは生成されません")
            q = (body or {}).get("query")
            if not isinstance(q, str) or not q.strip():
                c.error(w, f"{platform}.query が必要です")
                continue
            unknown = set(re.findall(r"\{(\w+)\}", q)) - KNOWN_PLACEHOLDERS
            if unknown:
                c.error(w, "未知のプレースホルダ: " + ", ".join(sorted("{" + u + "}" for u in unknown)))

    web = cfg.web
    jd = web.get("job_defaults", {})
    if not isinstance(jd, dict):
        c.error(f"{f}: web.job_defaults", "辞書ではありません")
    excludes = web.get("excludes") or {}
    if not isinstance(excludes, dict):
        c.error(f"{f}: web.excludes", "辞書ではありません")
        excludes = {}
    for name, spec in excludes.items():
        w = f"{f}: web.excludes.{name}"
        if not isinstance(spec, dict) or not isinstance(spec.get("auto"), bool):
            c.error(w, "auto: true/false が必要です")
            continue
        if not spec.get("filter"):
            c.error(w, "filter(フィルター本体)が必要です")
        else:
            check_filter_list(c, f"{w}.filter", spec["filter"])


# --- config/discord.yaml -----------------------------------------------------------------------

def check_discord(c: _Collector, cfg: Config) -> None:
    f = "config/discord.yaml"
    if not cfg.discord:
        c.warn(f, "ファイルがありません(通知先が決まっていないため、実際の送信はできません)")
        return
    d = cfg.discord.get("discord")
    if not isinstance(d, dict):
        c.error(f, "最上位に discord: が必要です")
        return
    api = d.get("api_base")
    if api is not None and not (isinstance(api, str) and api.startswith(("http://", "https://"))):
        c.error(f"{f}: discord.api_base", "http(s) のURLにしてください")
    ch = d.get("channels")
    if not isinstance(ch, dict):
        c.error(f"{f}: discord.channels", "辞書が必要です")
        return
    seen_values: dict[str, list[str]] = {}

    def check_id(where: str, value) -> None:
        if value is None:
            return
        text = str(value).strip()
        if not CHANNEL_ID.match(text):
            c.error(where, f"チャンネルIDは17〜20桁の数字です: {value!r}(YAMLでは数字が丸められるので、\"\" で囲むのが安全)")
        elif isinstance(value, int):
            c.warn(where, "数字のまま書かれています。桁が大きいので \"…\" で囲んでください(丸め・書式の事故防止)")

    for key in ("fallback", "errors"):
        check_id(f"{f}: discord.channels.{key}", ch.get(key))
    for tid, value in (ch.get("types") or {}).items():
        if tid not in (cfg.common.get("company_types") or {}):
            c.error(f"{f}: discord.channels.types.{tid}", "common.yaml の company_types にない種別です(綴りの間違い?)")
        check_id(f"{f}: discord.channels.types.{tid}", value)
    roles = ch.get("roles") or {}
    if not isinstance(roles, dict):
        c.error(f"{f}: discord.channels.roles", "辞書が必要です")
        return
    for rid, value in roles.items():
        if rid not in cfg.job_roles:
            c.error(f"{f}: discord.channels.roles.{rid}", "common.yaml の job_roles にない職種IDです(綴りの間違い?)")
        check_id(f"{f}: discord.channels.roles.{rid}", value)
        if value is not None:
            seen_values.setdefault(str(value), []).append(str(rid))
    for cid, rids in seen_values.items():
        if len(rids) > 1:
            c.warn(f"{f}: discord.channels.roles", f"同じチャンネルに複数の職種があります(1回しか送りません): {', '.join(rids)}")
    mapped = {r for r, v in roles.items() if v is not None}
    used = {r for co in cfg.companies for r in _company_role_ids(co)}
    unmapped = sorted(r for r in used if r not in mapped)
    if unmapped:
        if ch.get("fallback") is None:
            c.warn(f, f"チャンネル未割り当ての職種があり、fallback も未設定のため、通知先がありません: {', '.join(unmapped)}")
        else:
            c.warn(f, f"チャンネル未割り当ての職種は fallback に送られます: {', '.join(unmapped)}")


def _company_role_ids(company: dict) -> list[str]:
    from .config import company_role_ids
    return company_role_ids(company)


# --- companies/*.yaml --------------------------------------------------------------------------

def _valid_uuid(value) -> bool:
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value.lower()
    except ValueError:
        return False


def check_company(c: _Collector, cfg: Config, company: dict) -> None:
    f = company.get("_file", "?")
    stem = Path(company.get("_file", "")).stem

    for key in sorted(set(company) - COMPANY_KEYS):
        c.warn(f, f"知らない項目です(綴りの間違い?): {key}")
    ctype = company.get("type", "studio")
    if ctype not in (cfg.common.get("company_types") or {}):
        c.error(f, f"type は common.yaml の company_types にあるものにしてください(portal など): {ctype!r}")
    elif company.get("_folder") == "companies" and ctype != "studio":
        c.error(f, f"db/companies/ にはアニメ会社(studio)だけ置けます。type: {ctype} のファイルは db/portals/ へ移してください")
    elif company.get("_folder") == "portals" and ctype == "studio":
        c.error(f, "db/portals/ はアニメ会社ではない窓口(就活サイト・SNSなど)用です。アニメ会社なら db/companies/ へ移してください")
    if company.get("schema_version") != 1:
        c.error(f, f"schema_version は 1 にしてください(現在: {company.get('schema_version')!r})")
    if not _valid_uuid(company.get("id")):
        c.error(f, f"id は UUID(例: 24c7cee1-2d78-4ad4-b145-f5ba4ff7fca4)にしてください: {company.get('id')!r}")
    slug = company.get("slug")
    if not isinstance(slug, str) or not SLUG.match(slug):
        c.error(f, f"slug は小文字英数字とハイフンだけにしてください: {slug!r}")
    elif slug != stem:
        c.error(f, f"slug「{slug}」とファイル名「{stem}.yaml」が一致しません")
    for key in ("name", "display_name"):
        if not isinstance(company.get(key), str) or not company[key].strip():
            c.error(f, f"{key} が必要です")
    year = company.get("founded_year")
    if year is not None and (not isinstance(year, int) or isinstance(year, bool) or not 1900 <= year <= dt.date.today().year):
        c.error(f, f"founded_year は 1900〜今年の整数か null にしてください: {year!r}")
    if company.get("location") is not None and not isinstance(company.get("location"), dict):
        c.error(f, "location(所在地)は辞書にしてください")
    elif company.get("location") is None and ctype == "studio":
        c.error(f, "location(所在地)が必要です(分からない項目は null)")
    site = company.get("website")
    if site is not None and not (isinstance(site, str) and re.match(r"^https?://\S+$", site)):
        c.error(f, f"website は http(s) のURLか null にしてください: {site!r}")

    # roles
    seen_roles: set[str] = set()
    raw_roles = company.get("roles")
    if not isinstance(raw_roles, list):
        c.error(f, "roles はリストにしてください")
        raw_roles = []
    for i, item in enumerate(raw_roles):
        w = f"{f}: roles[{i}]"
        if isinstance(item, str):
            rid, adj = item, None
        elif isinstance(item, dict) and isinstance(item.get("id"), str):
            rid, adj = item["id"], item.get("exclude_words")
        else:
            c.error(w, "職種IDの文字列、または {id: …, exclude_words: …} にしてください")
            continue
        if rid not in cfg.job_roles:
            c.error(w, f"common.yaml の job_roles にない職種IDです: {rid}")
        if rid in seen_roles:
            c.error(w, f"職種が重複しています: {rid}")
        seen_roles.add(rid)
        if adj is not None:
            if not isinstance(adj, dict) or set(adj) - {"add", "remove"}:
                c.error(w, "exclude_words は {add: […], remove: […]} の形にしてください")
            else:
                base = (cfg.job_roles.get(rid) or {}).get("exclude_words") or []
                for word in adj.get("remove") or []:
                    if word not in base:
                        c.warn(w, f"remove の「{word}」は、共通の除外語にありません")
                kws = (cfg.job_roles.get(rid) or {}).get("keywords") or []
                for word in adj.get("add") or []:
                    if any(word in k for k in kws):
                        c.error(w, f"add の「{word}」は、その職種のキーワードに含まれるため使えません")
                    if '"' in word:
                        c.error(w, f'除外語に " は使えません: {word}')

    # related_companies
    for i, rel in enumerate(company.get("related_companies") or []):
        w = f"{f}: related_companies[{i}]"
        if not isinstance(rel, dict) or not rel.get("name"):
            c.error(w, "name が必要です")
            continue
        rid = rel.get("company_id")
        if rid is not None:
            if not _valid_uuid(rid):
                c.error(w, f"company_id は UUID か null にしてください: {rid!r}")
            elif rid not in {co.get("id") for co in cfg.companies}:
                c.warn(w, "company_id に対応する会社ファイルがありません")

    check_sns(c, cfg, company, f)
    check_monitors(c, cfg, company, f, seen_roles)


def check_sns(c: _Collector, cfg: Config, company: dict, f: str) -> None:
    platforms = cfg.sns.get("platforms") or {}
    sns = company.get("sns")
    if not isinstance(sns, dict):
        c.error(f, "sns が必要です(アカウントが無くても accounts: {} などで書く)")
        return
    accounts = sns.get("accounts") or {}
    if not isinstance(accounts, dict):
        c.error(f"{f}: sns.accounts", "辞書にしてください")
        accounts = {}
    any_monitor = False
    for platform, lst in accounts.items():
        w = f"{f}: sns.accounts.{platform}"
        if platform not in platforms:
            c.error(w, "common.yaml の sns.platforms にないプラットフォームです")
            continue
        if not isinstance(lst, list):
            c.error(w, "アカウントのリストにしてください")
            continue
        handles: set[str] = set()
        for i, acc in enumerate(lst):
            wa = f"{w}[{i}]"
            if not isinstance(acc, dict):
                c.error(wa, "辞書(handle / label / monitor)にしてください")
                continue
            for k in set(acc) - {"handle", "label", "monitor"}:
                c.warn(wa, f"知らない項目です: {k}")
            raw = acc.get("handle")
            handle = normalize_handle(raw)
            monitor = acc.get("monitor", False)
            if not isinstance(monitor, bool):
                c.error(wa, f"monitor は true/false にしてください: {monitor!r}")
            if handle is None:
                if monitor is True:
                    c.warn(wa, "monitor: true ですが handle が未設定です(監視されません)")
                continue
            if not isinstance(raw, str):
                c.error(wa, f"handle は文字列にしてください: {raw!r}")
                continue
            if not HANDLE.match(handle):
                c.error(wa, f"handle の形式が不正です(URLではなくIDだけ、@なしで書く): {raw!r}")
            if handle.lower() in handles:
                c.error(wa, f"同じアカウントが重複しています: {handle}")
            handles.add(handle.lower())
            if acc.get("label") is not None and not isinstance(acc["label"], str):
                c.error(wa, "label は文字列にしてください")
            if monitor is True:
                any_monitor = True

    search = sns.get("search") or {}
    if not isinstance(search, dict):
        c.error(f"{f}: sns.search", "辞書にしてください")
        return
    base_inc = (cfg.sns.get("default_filter") or {}).get("include") or []
    base_exc = (cfg.sns.get("default_filter") or {}).get("exclude") or []
    adj = search.get("default_filter") or {}
    for side, base in (("include", base_inc), ("exclude", base_exc)):
        a = adj.get(side) or {}
        w = f"{f}: sns.search.default_filter.{side}"
        if not isinstance(a, dict) or set(a) - {"add", "remove"}:
            c.error(w, "{add: […], remove: […]} の形にしてください")
            continue
        for key in ("add", "remove"):
            if not _is_str_list(a.get(key) or []):
                c.error(w, f"{key} は文字列のリストにしてください")
        for word in a.get("remove") or []:
            if word not in base:
                c.warn(w, f"remove の「{word}」は、共通の語にありません")
        for word in a.get("add") or []:
            if word in base:
                c.warn(w, f"add の「{word}」は、すでに共通の語にあります")
    from .rsshub import effective_filter
    inc, exc = effective_filter(cfg, company)
    if any_monitor and not inc and company.get("type", "studio") == "studio":   # portal(就活情報アカウントなど)は、全投稿を通知してよい
        c.error(f"{f}: sns.search.default_filter.include", "監視するアカウントがあるのに、含める語が空です")
    for word in set(inc) & set(exc):
        c.error(f"{f}: sns.search.default_filter", f"「{word}」が include と exclude の両方にあります")

    kws = search.get("keywords") or []
    if not _is_str_list(kws):
        c.error(f"{f}: sns.search.keywords", "文字列のリストにしてください")
    presets = search.get("use_presets") or []
    if not isinstance(presets, list):
        c.error(f"{f}: sns.search.use_presets", "リストにしてください")
        presets = []
    for name in presets:
        if name not in (cfg.sns.get("search_presets") or {}):
            c.error(f"{f}: sns.search.use_presets", f"common.yaml にないプリセットです: {name}")
    if search.get("custom") is not None and not isinstance(search.get("custom"), dict):
        c.error(f"{f}: sns.search.custom", "辞書にしてください")
        return

    # 実際にフィードを組み立てて、組み立てエラー(クエリが長すぎる等)を拾う
    try:
        _, errs = company_feeds(cfg, company, "https://rsshub.invalid")
    except Exception as e:  # 想定外の形の設定でも検査を止めない
        c.error(f, f"フィードを組み立てられません: {type(e).__name__}: {e}")
        return
    for label, reason in errs:
        c.error(f"{f}: {label}", reason)


def check_monitors(c: _Collector, cfg: Config, company: dict, f: str, company_roles: set[str]) -> None:
    monitors = company.get("monitors")
    if monitors is None:
        return
    if not isinstance(monitors, list):
        c.error(f"{f}: monitors", "リストにしてください")
        return
    excludes = cfg.web.get("excludes") or {}
    ids: set[str] = set()
    urls: set[str] = set()
    for i, m in enumerate(monitors):
        w = f"{f}: monitors[{i}]"
        if not isinstance(m, dict):
            c.error(w, "辞書にしてください")
            continue
        mid = m.get("id")
        if not isinstance(mid, str) or not SLUG.match(mid):
            c.error(w, f"id は小文字英数字とハイフンにしてください: {mid!r}")
        elif mid in ids:
            c.error(w, f"id が重複しています: {mid}")
        ids.add(str(mid))
        if not isinstance(m.get("name"), str) or not m["name"].strip():
            c.error(w, "name が必要です")
        url = m.get("url")
        if not isinstance(url, str) or not re.match(r"^https?://\S+$", url):
            c.error(w, f"url は http(s) のURLにしてください: {url!r}")
        elif url in urls:
            c.warn(w, f"同じURLが重複しています: {url}")
        urls.add(str(url))
        if m.get("job_name") is not None and (not isinstance(m["job_name"], str) or not m["job_name"].strip()):
            c.error(w, "job_name は空でない文字列にしてください")
        if m.get("command") is not None and (not isinstance(m["command"], str) or not m["command"].strip()):
            c.error(w, "command は空でない文字列にしてください(url は表示用として必要です)")
        if "enabled" in m and not isinstance(m["enabled"], bool):
            c.error(w, "enabled は true/false にしてください")
        mroles = m.get("roles") or []
        if not isinstance(mroles, list):
            c.error(w, "roles はリストにしてください")
            mroles = []
        for r in mroles:
            if r not in company_roles:
                c.error(w, f"roles の「{r}」が、この会社の roles にありません")
        for name in m.get("snippets") or []:
            if name not in excludes:
                c.error(w, f"snippets の「{name}」は、common.yaml の web.excludes にありません")
            elif (excludes[name] or {}).get("auto"):
                c.warn(w, f"snippets の「{name}」は auto: true なので、書かなくても適用されます")
        skip = m.get("skip_common_excludes") or []
        if skip != "all":
            for name in skip if isinstance(skip, list) else [skip]:
                if name != "all" and name not in excludes:
                    c.error(w, f"skip_common_excludes の「{name}」は、common.yaml の web.excludes にありません")
        uw = m.get("urlwatch")
        if uw is not None:
            if not isinstance(uw, dict):
                c.error(f"{w}.urlwatch", "辞書にしてください")
                continue
            for k in ("name", "url"):
                if k in uw:
                    c.error(f"{w}.urlwatch", f"{k} は自動で組み立てるので書かないでください")
            if "filter" in uw:
                check_filter_list(c, f"{w}.urlwatch.filter", uw["filter"])
            for k in ("max_tries", "timeout"):
                if k in uw and (not isinstance(uw[k], int) or isinstance(uw[k], bool) or uw[k] < 1):
                    c.error(f"{w}.urlwatch", f"{k} は1以上の整数にしてください: {uw[k]!r}")


def check_cross(c: _Collector, cfg: Config) -> None:
    """会社をまたいだ重複。"""
    by_id: dict[str, str] = {}
    by_slug: dict[str, str] = {}
    by_name: dict[str, str] = {}
    by_job: dict[str, str] = {}
    for co in cfg.companies:
        f = co.get("_file", "?")
        for table, key, label in ((by_id, co.get("id"), "id"), (by_slug, co.get("slug"), "slug")):
            if key in table:
                c.error(f, f"{label} が {table[key]} と重複しています: {key}")
            elif key:
                table[key] = f
        dn = co.get("display_name")
        if dn in by_name:
            c.warn(f, f"表示名が {by_name[dn]} と同じです: {dn}(通知で区別できません)")
        elif dn:
            by_name[dn] = f
        for m in co.get("monitors") or []:
            if isinstance(m, dict) and m.get("name") and dn:
                jn = job_name(m, co)
                if jn in by_job:
                    c.error(f, f"urlwatch のジョブ名が {by_job[jn]} と重複しています(状態が混ざります): {jn}")
                else:
                    by_job[jn] = f


def validate_config(root: Path | None = None, **paths) -> list[Issue]:
    c = _Collector()
    try:
        cfg = load_config(root, **paths)
    except ConfigError as e:
        c.error("(読み込み)", str(e))
        return c.issues
    check_common(c, cfg)
    check_discord(c, cfg)
    for company in cfg.companies:
        check_company(c, cfg, company)
    check_cross(c, cfg)
    if not cfg.companies:
        c.warn("db/companies/", "会社ファイルが1つもありません")
    return c.issues
