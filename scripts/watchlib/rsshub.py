"""会社ファイル → RSSHub のフィードURL(と、取得・解析)。

フィードの種類:
  user    公式アカウントのフィード(monitor: true のものだけ)。既定フィルター(採用ワード)付き
  search  検索フィード(use_presets / custom を書いた会社だけ)。X のキーワード検索ルートなど
"""
from __future__ import annotations

import html
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Callable

from .config import Config, apply_adjust, company_role_entries, normalize_handle, role_exclude_words

USER_AGENT = "website-checker/1.0 (+https://github.com/HalNoDaifuku/website-checker)"
KNOWN_PLACEHOLDERS = {"company_keywords", "role_keywords", "role_clauses"}
_PLACEHOLDER = re.compile(r"\{(\w+)\}")


class FeedBuildError(Exception):
    """フィードを組み立てられない(クエリが長すぎる、など)。"""


@dataclass
class Feed:
    key: str                    # 状態ファイル用の安定したキー(例: cloverworks:x:user:CloverWorks)
    company_id: str
    company_slug: str
    company_name: str
    platform: str
    platform_label: str
    kind: str                   # "user" | "search"
    label: str                  # 通知に出す名前(アカウントのlabel、プリセット名など)
    url: str                    # 表示用のURL(アクセスキーなし)
    request_url: str            # 実際に取得するURL(アクセスキー付き)。ログに出さない
    hint_roles: list[str] = field(default_factory=list)
    direct: str = ""            # "youtube": RSSHub を通さず、YouTube の公式フィードを直接読む
    handle: str = ""
    include: list[str] = field(default_factory=list)   # direct のとき、こちらで絞る語(RSSHub の filter の代わり)
    exclude: list[str] = field(default_factory=list)


@dataclass
class Item:
    id: str
    url: str
    text: str
    published: str | None


# --- URL の組み立て ----------------------------------------------------------------------

def _alternation(words: list[str]) -> str:
    return "|".join(re.escape(w) for w in words)


def effective_filter(cfg: Config, company: dict) -> tuple[list[str], list[str]]:
    """既定フィルターに、会社ごとの add / remove を反映した (include, exclude)。"""
    base = cfg.sns.get("default_filter") or {}
    adj = ((company.get("sns") or {}).get("search") or {}).get("default_filter") or {}
    include = apply_adjust(base.get("include") or [], adj.get("include"))
    exclude = apply_adjust(base.get("exclude") or [], adj.get("exclude"))
    return include, exclude


def filter_query(cfg: Config, include: list[str], exclude: list[str]) -> str:
    template = (cfg.sns.get("default_filter") or {}).get("rsshub_query") or "filter={include}&filterout={exclude}"
    parts = []
    for part in template.split("&"):
        if "{exclude}" in part and not exclude:
            continue
        if "{include}" in part and not include:
            continue
        part = part.replace("{include}", urllib.parse.quote(_alternation(include), safe=""))
        part = part.replace("{exclude}", urllib.parse.quote(_alternation(exclude), safe=""))
        parts.append(part)
    return "&".join(parts)


def _with_query(url: str, *queries: str) -> str:
    extra = "&".join(q for q in queries if q)
    if not extra:
        return url
    return url + ("&" if "?" in url else "?") + extra


def _finish(url: str, access_key: str | None) -> tuple[str, str]:
    """(表示用URL, 取得用URL)。JSON Feed 形式にし、アクセスキーは取得用にだけ付ける。"""
    shown = _with_query(url, "format=json")
    request = _with_query(shown, f"key={urllib.parse.quote(access_key, safe='')}" if access_key else "")
    return shown, request


def build_user_feed(cfg: Config, platform: str, handle: str, include: list[str], exclude: list[str],
                    base: str, access_key: str | None) -> tuple[str, str]:
    p = (cfg.sns.get("platforms") or {}).get(platform) or {}
    clean = normalize_handle(handle)
    if not clean:
        raise FeedBuildError("アカウントIDが空です")
    template = p.get("user_feed")
    prefix = p.get("handle_prefix") or ""
    channel_tpl, channel_pat = p.get("channel_feed"), p.get("channel_id_pattern")
    if channel_tpl and channel_pat and re.fullmatch(channel_pat, clean):  # YouTube のチャンネルID(UC…)は専用のルートを使う
        template, prefix = channel_tpl, ""
    if not template:
        raise FeedBuildError(f"{platform}: user_feed が未定義です")
    url = template.format(base=base.rstrip("/"), handle=urllib.parse.quote(prefix + clean, safe="@"))
    extra = (p.get("user_params") or "") if cfg.sns.get("exclude_retweets", True) else ""
    return _finish(_with_query(url, extra, filter_query(cfg, include, exclude)), access_key)


# --- YouTube を直接読む(RSSHub を通さない) ---------------------------------------------------

YT_CHANNEL_ID = re.compile(r"UC[0-9A-Za-z_-]{22}")
_YT_BROWSER_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; website-checker/1.0)",
                       "Accept-Language": "ja,en;q=0.8", "Cookie": "CONSENT=YES+1; SOCS=CAI"}


def build_direct_youtube(p: dict, handle: str) -> tuple[str, str]:
    """(表示用URL, 取得用URL)。チャンネルID(UC…)なら公式フィードのURL、@ID ならチャンネルのページ(IDを調べる用)。"""
    clean = normalize_handle(handle)
    if YT_CHANNEL_ID.fullmatch(clean):
        feed = (p.get("direct_feed") or "https://www.youtube.com/feeds/videos.xml?channel_id={id}").format(id=clean)
        return f"https://www.youtube.com/channel/{clean}", feed
    page = (p.get("handle_page") or "https://www.youtube.com/@{handle}").format(handle=urllib.parse.quote(clean, safe=""))
    return page, page


def _http_get(url: str, headers: dict, timeout: int, opener: Callable | None = None) -> str:
    request = urllib.request.Request(url, headers=headers)
    try:
        with (opener or urllib.request.urlopen)(request, timeout=timeout) as response:
            return response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raise FeedFetchError(f"HTTP {e.code}") from None
    except urllib.error.URLError as e:
        if isinstance(e.reason, TimeoutError):
            raise FeedFetchError("タイムアウト") from None
        raise FeedFetchError(f"接続できません: {e.reason}") from None
    except TimeoutError:
        raise FeedFetchError("タイムアウト") from None


_YT_ID_IN_PAGE = (re.compile(r'channel_id=(UC[0-9A-Za-z_-]{22})'),
                  re.compile(r'"(?:channelId|externalId|browseId)"\s*:\s*"(UC[0-9A-Za-z_-]{22})"'),
                  re.compile(r'youtube\.com/channel/(UC[0-9A-Za-z_-]{22})'))


def resolve_youtube_channel_id(page_url: str, *, timeout: int = 30, opener: Callable | None = None) -> str:
    html_text = _http_get(page_url, _YT_BROWSER_HEADERS, timeout, opener)
    for pattern in _YT_ID_IN_PAGE:
        m = pattern.search(html_text)
        if m:
            return m.group(1)
    raise FeedFetchError("チャンネルIDを調べられません(@IDが違うか、YouTube側の表示が変わった可能性)")


_ATOM = "{http://www.w3.org/2005/Atom}"
_MEDIA = "{http://search.yahoo.com/mrss/}"
_YT = "{http://www.youtube.com/xml/schemas/2015}"


def parse_youtube_atom(raw: str) -> list[Item]:
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        raise FeedFetchError("YouTube のフィードとして読めません") from None
    items: list[Item] = []
    for entry in root.findall(f"{_ATOM}entry"):
        vid = (entry.findtext(f"{_YT}videoId") or "").strip()
        link = entry.find(f"{_ATOM}link")
        url = (link.get("href") if link is not None else "") or (f"https://www.youtube.com/watch?v={vid}" if vid else "")
        title = (entry.findtext(f"{_ATOM}title") or "").strip()
        desc = ""
        group = entry.find(f"{_MEDIA}group")
        if group is not None:
            desc = (group.findtext(f"{_MEDIA}description") or "").strip()
        iid = (entry.findtext(f"{_ATOM}id") or vid or url).strip()
        if not iid:
            continue
        text = title + ("\n" + desc if desc else "")
        items.append(Item(iid, url, text.strip(), entry.findtext(f"{_ATOM}published") or entry.findtext(f"{_ATOM}updated")))
    return items


def filter_items(items: list[Item], include: list[str], exclude: list[str]) -> list[Item]:
    """RSSHub の filter / filterout と同じ考え方で、本文(題名+説明)に include のどれかを含み、exclude のどれも含まないものだけ残す。"""
    inc = re.compile(_alternation(include)) if include else None
    exc = re.compile(_alternation(exclude)) if exclude else None
    return [i for i in items if (not inc or inc.search(i.text)) and not (exc and exc.search(i.text))]


def fetch_youtube(feed: Feed, *, resolved: dict | None = None, timeout: int = 30, opener: Callable | None = None) -> list[Item]:
    """YouTube の公式フィード(videos.xml)を直接取得して、採用ワードで絞る。resolved は {ハンドル: チャンネルID} の控え。"""
    resolved = resolved if resolved is not None else {}
    feed_url = feed.request_url
    if "channel_id=" not in feed_url:   # @ID のとき: チャンネルIDを調べる(1度調べたら控えを使う)
        cid = resolved.get(feed.handle)
        if not cid:
            cid = resolve_youtube_channel_id(feed.request_url, timeout=timeout, opener=opener)
            resolved[feed.handle] = cid
        feed_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}"
    raw = _http_get(feed_url, _YT_BROWSER_HEADERS, timeout, opener)
    return filter_items(parse_youtube_atom(raw), feed.include, feed.exclude)


def _api_json(url: str, timeout: int, opener: Callable | None) -> dict:
    raw = _http_get(url, {"User-Agent": "website-checker/1.0", "Accept": "application/json"}, timeout, opener)
    try:
        data = json.loads(raw)
    except ValueError:
        raise FeedFetchError("YouTube API の応答が読めません") from None
    return data if isinstance(data, dict) else {}


def fetch_youtube_api(feed: Feed, api_key: str | None, *, resolved: dict | None = None, timeout: int = 30,
                      opener: Callable | None = None, max_results: int = 15) -> list[Item]:
    """YouTube Data API(公式)で、チャンネルの最新のアップロードを取得し、採用ワードで絞る。
    @ID は channels.list(forHandle)で一度だけチャンネルIDにして控える。1フィード=1〜2ユニット(1日の無料枠は10,000)。
    エラー文にAPIキー(URL)は含めない。"""
    if not api_key:
        raise FeedFetchError("YOUTUBE_API_KEY が未設定です(GitHub の Secrets に登録)")
    resolved = resolved if resolved is not None else {}
    q = urllib.parse.quote
    clean = normalize_handle(feed.handle) or ""
    cid = clean if YT_CHANNEL_ID.fullmatch(clean) else resolved.get(clean)
    if not cid:
        data = _api_json(f"https://www.googleapis.com/youtube/v3/channels?part=id&forHandle={q('@' + clean, safe='')}&key={q(api_key, safe='')}",
                         timeout, opener)
        found = [i.get("id") for i in data.get("items") or [] if isinstance(i, dict) and i.get("id")]
        if not found:
            raise FeedFetchError("チャンネルが見つかりません(@IDの間違いの可能性)")
        cid = resolved[clean] = found[0]
    playlist = "UU" + cid[2:]   # アップロード一覧のプレイリストID
    try:
        data = _api_json("https://www.googleapis.com/youtube/v3/playlistItems?part=snippet,contentDetails"
                         f"&maxResults={max_results}&playlistId={q(playlist, safe='')}&key={q(api_key, safe='')}", timeout, opener)
    except FeedFetchError as e:
        if str(e) == "HTTP 404":   # 動画が1本もないチャンネルは、アップロード一覧が無く 404 になる。エラーにせず「新着なし」とする
            return []
        raise
    items: list[Item] = []
    for raw in data.get("items") or []:
        sn = (raw or {}).get("snippet") or {}
        vid = ((raw.get("contentDetails") or {}).get("videoId")) or ((sn.get("resourceId") or {}).get("videoId")) or ""
        title = (sn.get("title") or "").strip()
        if not vid or title in ("Private video", "Deleted video"):
            continue
        desc = (sn.get("description") or "").strip()
        published = (raw.get("contentDetails") or {}).get("videoPublishedAt") or sn.get("publishedAt")
        items.append(Item(f"yt:video:{vid}", f"https://www.youtube.com/watch?v={vid}",
                          (title + ("\n" + desc if desc else "")).strip(), published))
    return filter_items(items, feed.include, feed.exclude)


# --- 検索クエリ ---------------------------------------------------------------------------

def _term(word: str) -> str:
    return f'"{word}"' if re.search(r"\s", word) else word


def or_join(words: list[str]) -> str:
    return " OR ".join(_term(w) for w in words)


def role_clause(keywords: list[str], excludes: list[str]) -> str:
    """((kw OR kw) -"除外1" -"除外2")。除外語がなければ (kw OR kw)。"""
    body = f"({or_join(keywords)})"
    if not excludes:
        return body
    return "(" + body + "".join(f' -"{w}"' for w in excludes) + ")"


def _role_data(cfg: Config, company: dict) -> list[tuple[str, list[str], list[str]]]:
    out = []
    for entry in company_role_entries(company):
        role = cfg.job_roles.get(entry["id"])
        if role and role.get("keywords"):
            out.append((entry["id"], list(role["keywords"]), role_exclude_words(cfg.job_roles, entry)))
    return out


def expand_query(cfg: Config, company: dict, template: str) -> list[tuple[str, list[str]]]:
    """プリセットのクエリを展開する。{role_clauses} があれば、長さの上限で複数のクエリに分割。

    戻り値: [(クエリ, そのクエリに含まれる職種ID), ...]
    """
    search = (company.get("sns") or {}).get("search") or {}
    limit = int(cfg.sns.get("search_max_query_length") or 480)
    suffix = (cfg.sns.get("search_query_suffix") or "").strip()
    names = set(_PLACEHOLDER.findall(template))
    unknown = names - KNOWN_PLACEHOLDERS
    if unknown:
        raise FeedBuildError(f"未知のプレースホルダ: {', '.join(sorted('{' + n + '}' for n in unknown))}")

    company_kw = list(search.get("keywords") or [])
    if "company_keywords" in names and not company_kw:
        raise FeedBuildError("{company_keywords} を使うには、sns.search.keywords が必要です")
    roles = _role_data(cfg, company)
    if ({"role_keywords", "role_clauses"} & names) and not roles:
        raise FeedBuildError("職種のキーワードを使うには、roles に職種が必要です")

    def render(role_part_clauses: list[str], role_kw: list[str]) -> str:
        q = template
        q = q.replace("{company_keywords}", or_join(company_kw))
        q = q.replace("{role_keywords}", or_join(role_kw))
        q = q.replace("{role_clauses}", " OR ".join(role_part_clauses))
        return (q + (" " + suffix if suffix else "")).strip()

    if "role_clauses" not in names:
        query = render([], [k for _, kws, _ in roles for k in kws])
        if len(query) > limit:
            raise FeedBuildError(f"クエリが長すぎます({len(query)}文字 > {limit}文字)")
        return [(query, [r for r, _, _ in roles])]

    chunks: list[tuple[list[str], list[str]]] = []
    cur_clauses: list[str] = []
    cur_roles: list[str] = []
    all_kw = [k for _, kws, _ in roles for k in kws]
    for rid, kws, exs in roles:
        clause = role_clause(kws, exs)
        if len(render([clause], all_kw)) > limit:
            raise FeedBuildError(f"職種 {rid} 1つぶんだけでクエリが長すぎます(除外語を減らしてください)")
        if cur_clauses and len(render(cur_clauses + [clause], all_kw)) > limit:
            chunks.append((cur_clauses, cur_roles))
            cur_clauses, cur_roles = [], []
        cur_clauses.append(clause)
        cur_roles.append(rid)
    if cur_clauses:
        chunks.append((cur_clauses, cur_roles))
    return [(render(c, all_kw), r) for c, r in chunks]


# --- 会社 → フィード一覧 -------------------------------------------------------------------

def company_feeds(cfg: Config, company: dict, base: str, access_key: str | None = None,
                  apply_filter: bool = True) -> tuple[list[Feed], list[tuple[str, str]]]:
    """1社ぶんのフィード。組み立てられなかったものは errors [(ラベル, 理由)] に入れる。"""
    platforms = cfg.sns.get("platforms") or {}
    slug = company.get("slug", "?")
    name = company.get("display_name") or company.get("name") or slug
    cid = company.get("id", "")
    sns = company.get("sns") or {}
    feeds: list[Feed] = []
    errors: list[tuple[str, str]] = []
    include, exclude = effective_filter(cfg, company)
    if not apply_filter:  # 動作確認用: 採用ワードで絞らず、最新の投稿をそのまま取る
        include, exclude = [], []

    for platform, accounts in (sns.get("accounts") or {}).items():
        p = platforms.get(platform)
        if not p:
            errors.append((f"{slug}:{platform}", "common.yaml に無いプラットフォームです"))
            continue
        for acc in accounts or []:
            handle = normalize_handle((acc or {}).get("handle"))
            if not handle or not (acc or {}).get("monitor"):
                continue
            label = (acc.get("label") or "").strip() or f"@{handle}"
            if p.get("enabled") is False:   # common.yaml でプラットフォームごと止めている
                continue
            if p.get("direct") in ("youtube", "youtube_api"):
                shown, req = build_direct_youtube(p, handle)
                feeds.append(Feed(f"{slug}:{platform}:user:{handle}", cid, slug, name, platform,
                                  p.get("label", platform), "user", label, shown, req,
                                  direct=p["direct"], handle=handle, include=list(include), exclude=list(exclude)))
                continue
            try:
                shown, req = build_user_feed(cfg, platform, handle, include, exclude, base, access_key)
            except (FeedBuildError, KeyError, IndexError) as e:
                errors.append((f"{slug}:{platform}:{handle}", str(e)))
                continue
            feeds.append(Feed(f"{slug}:{platform}:user:{handle}", cid, slug, name, platform,
                              p.get("label", platform), "user", label, shown, req))

    search = sns.get("search") or {}
    queries: list[tuple[str, str, str, list[str]]] = []  # (key, platform, query, roles)
    presets = cfg.sns.get("search_presets") or {}
    for preset in search.get("use_presets") or []:
        spec = presets.get(preset)
        if not spec:
            errors.append((f"{slug}:preset:{preset}", "common.yaml に無いプリセットです"))
            continue
        for platform in [k for k in spec if k != "description"]:
            tpl = (spec.get(platform) or {}).get("query")
            if not tpl:
                continue
            try:
                for i, (q, roles) in enumerate(expand_query(cfg, company, tpl), 1):
                    queries.append((f"{slug}:{platform}:search:{preset}:{i}", platform, q, roles))
            except FeedBuildError as e:
                errors.append((f"{slug}:{platform}:search:{preset}", str(e)))
    for cname, spec in (search.get("custom") or {}).items():
        platform = (spec or {}).get("platform")
        q = ((spec or {}).get("query") or "").strip()
        limit = int(cfg.sns.get("search_max_query_length") or 480)
        if not platform or not q:
            errors.append((f"{slug}:custom:{cname}", "platform と query が必要です"))
        elif len(q) > limit:
            errors.append((f"{slug}:custom:{cname}", f"クエリが長すぎます({len(q)}文字 > {limit}文字)"))
        else:
            queries.append((f"{slug}:{platform}:search:custom:{cname}", platform, q, []))

    for key, platform, q, roles in queries:
        p = platforms.get(platform) or {}
        template = p.get("search_feed")
        if not template:
            errors.append((key, f"{platform} には検索フィードが定義されていません"))
            continue
        url = template.format(base=base.rstrip("/"), query=urllib.parse.quote(q, safe=""))
        shown, req = _finish(url, access_key)
        label = key.split(":search:", 1)[1]
        feeds.append(Feed(key, cid, slug, name, platform, p.get("label", platform), "search", label,
                          shown, req, hint_roles=roles))
    return feeds, errors


def build_feeds(cfg: Config, base: str, access_key: str | None = None, only: str | None = None,
                apply_filter: bool = True) -> tuple[list[Feed], list[tuple[str, str]]]:
    feeds: list[Feed] = []
    errors: list[tuple[str, str]] = []
    for company in cfg.companies:
        if only and only not in (company.get("slug"), company.get("id")):
            continue
        f, e = company_feeds(cfg, company, base, access_key, apply_filter)
        feeds += f
        errors += e
    return feeds, errors


# --- 取得・解析 -------------------------------------------------------------------------------

_RETWEET = re.compile(r"^\s*(?:RT @|🔁)")


def is_retweet(text: str) -> bool:
    """本文が「RT @…」「🔁」で始まるリポスト。"""
    return bool(_RETWEET.match(text or ""))


def _html_to_text(value: str) -> str:
    value = re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", value)
    value = re.sub(r"(?s)<[^>]+>", "", value)
    return html.unescape(value).strip()


def parse_json_feed(data: dict) -> list[Item]:
    items: list[Item] = []
    for raw in data.get("items") or []:
        if not isinstance(raw, dict):
            continue
        url = raw.get("url") or raw.get("external_url") or ""
        iid = str(raw.get("id") or url or raw.get("title") or "")
        if not iid:
            continue
        text = raw.get("content_text") or ""
        if not text and raw.get("content_html"):
            text = _html_to_text(raw["content_html"])
        if not text:
            text = raw.get("title") or ""
        items.append(Item(iid, url, text.strip(), raw.get("date_published") or raw.get("date_modified")))
    return items


class FeedFetchError(Exception):
    pass


def fetch_feed(request_url: str, *, timeout: int = 30, opener: Callable | None = None) -> list[Item]:
    """RSSHub から JSON Feed を取得する。失敗は FeedFetchError(URLのアクセスキーは含めない)。"""
    request = urllib.request.Request(request_url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    open_ = opener or urllib.request.urlopen
    try:
        with open_(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raise FeedFetchError(f"HTTP {e.code}") from None
    except urllib.error.URLError as e:
        if isinstance(e.reason, TimeoutError):
            raise FeedFetchError("タイムアウト") from None
        raise FeedFetchError(f"接続できません: {e.reason}") from None
    except TimeoutError:
        raise FeedFetchError("タイムアウト") from None
    try:
        data = json.loads(raw)
    except ValueError:
        raise FeedFetchError("JSON Feed として読めません(RSSHubのURL・認証キーを確認)") from None
    if not isinstance(data, dict):
        raise FeedFetchError("JSON Feed の形式が不正です")
    return parse_json_feed(data)


def mask_key(url: str) -> str:
    return re.sub(r"([?&]key=)[^&]*", r"\1***", url)
