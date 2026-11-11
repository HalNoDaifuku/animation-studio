"""会社ファイルの monitors → urlwatch の urls.yaml。"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .config import Config


@dataclass
class JobInfo:
    name: str
    url: str
    company_slug: str
    company_name: str
    monitor_id: str
    category: str
    roles: list[str] = field(default_factory=list)  # 監視URLのroles(空=本文から職種を判定)
    is_command: bool = False                          # command ジョブ(url は表示用)


def job_name(monitor: dict, company: dict) -> str:
    if monitor.get("job_name"):  # 元の名前をそのまま使いたいとき(例: 「つくジョブ！」)
        return str(monitor["job_name"])
    return f"{monitor.get('name')} - {company.get('display_name') or company.get('name')}"


def common_excludes(cfg: Config, monitor: dict, stage: str = "html") -> list:
    """監視URLに挿入する共通Web除外フィルター(auto: true のもの + snippets、skip を除く)。

    stage: "html" は HTML のまま処理するもの(既定)、"text" は html2text のあとの文字を処理するもの。"""
    excludes = cfg.web.get("excludes") or {}
    skip = monitor.get("skip_common_excludes") or []
    skip_all = skip == "all" or "all" in (skip if isinstance(skip, list) else [])
    skip_names = set(skip) if isinstance(skip, list) else set()
    filters: list = []
    for name, spec in excludes.items():
        wanted = bool(spec.get("auto")) or name in (monitor.get("snippets") or [])
        if (spec.get("stage") or "html") != stage:
            continue
        if not wanted or (skip_all and spec.get("auto")) or name in skip_names:
            continue
        filters += copy.deepcopy(spec.get("filter") or [])
    return filters


def bot_guard_filter(cfg: Config) -> dict | None:
    """common.yaml の web.bot_challenge から、botguard フィルターを作る(無効・目印なしなら None)。"""
    c = cfg.web.get("bot_challenge") or {}
    markers = [str(m) for m in (c.get("markers") or []) if str(m)]
    if not c.get("enabled", True) or not markers:
        return None
    return {"botguard": "|".join(f"(?:{m})" for m in markers)}


def build_jobs(cfg: Config, only: str | None = None) -> tuple[list[dict], list[JobInfo]]:
    jobs: list[dict] = []
    infos: list[JobInfo] = []
    defaults = cfg.web.get("job_defaults") or {}
    for company in cfg.companies:
        if only and only not in (company.get("slug"), company.get("id")):
            continue
        for monitor in company.get("monitors") or []:
            if not monitor.get("enabled", True):
                continue
            name = job_name(monitor, company)
            command = monitor.get("command")
            job: dict = {"name": name, **({"command": command} if command else {"url": monitor["url"]})}
            if not command:  # command(shell)ジョブには、HTTP用の既定値(ignore_http_error_codes など)は使えない
                job.update(copy.deepcopy(defaults))
            own = copy.deepcopy(monitor.get("urlwatch") or {})
            own_filter = own.pop("filter", None) or []
            job.update(own)
            own_filter = list(own_filter)
            text_filters = common_excludes(cfg, monitor, "text")   # html2text のあとに入れる共通除外
            if text_filters:
                at = next((i for i, f in enumerate(own_filter) if f == "html2text" or (isinstance(f, dict) and "html2text" in f)), None)
                own_filter[(at + 1 if at is not None else len(own_filter)):(at + 1 if at is not None else len(own_filter))] = text_filters
            filters = common_excludes(cfg, monitor) + own_filter
            guard = bot_guard_filter(cfg)
            if guard and not command:
                filters.insert(0, guard)  # HTMLのまま、いちばん先に確認ページを検出する
            if filters:
                job["filter"] = filters
            jobs.append(job)
            infos.append(JobInfo(name, monitor["url"], company.get("slug", ""),
                                 company.get("display_name") or company.get("name", ""),
                                 monitor.get("id", ""), monitor.get("category", ""),
                                 list(monitor.get("roles") or []), bool(command)))
    return jobs, infos


def write_urls_yaml(jobs: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump_all(jobs, allow_unicode=True, sort_keys=False, explicit_start=True,
                              default_flow_style=False, width=1000)
    path.write_text(text, encoding="utf-8")


# urlwatch のフィルター → 必要なPythonモジュール(標準の依存に入っていないもの)
FILTER_MODULES = {"beautify": ["bs4"], "html2text": [], "css": ["cssselect", "lxml"], "xpath": ["lxml"]}


def missing_filter_modules(jobs: list[dict], find_spec=None) -> dict[str, list[str]]:
    """使うフィルターに必要なモジュールのうち、入っていないもの {モジュール: [使うジョブ名]}。

    入っていないと urlwatch はそのジョブを静かに失敗扱いにし、max_tries 回続くまで報告しないため、先に検出する。
    """
    import importlib.util
    find = find_spec or importlib.util.find_spec
    missing: dict[str, list[str]] = {}
    for job in jobs:
        for f in job.get("filter") or []:
            name = f if isinstance(f, str) else next(iter(f))
            for module in FILTER_MODULES.get(str(name).split(":", 1)[0], []):
                if find(module) is None:
                    missing.setdefault(module, [])
                    if job["name"] not in missing[module]:
                        missing[module].append(job["name"])
    return missing
