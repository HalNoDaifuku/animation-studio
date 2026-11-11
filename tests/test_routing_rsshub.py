import json
import unittest
from urllib.parse import parse_qs, unquote, urlsplit

import helpers
from helpers import MockRSSHub, load_sample, make_root
from watchlib import rsshub
from watchlib.config import load_config, normalize_handle
from watchlib.report import parse_report
from watchlib.routing import keyword_in_text, pick_roles, resolve_channels
from watchlib.state import State


def cfg_with(company_edit=None, slug="cloverworks"):
    company = load_sample(slug)
    for platform in ("youtube", "instagram", "bluesky"):   # 既定は X だけを対象にする(他は company_edit で足す)
        for acc in (company["sns"]["accounts"].get(platform) or []):
            acc["monitor"] = False
    if company_edit:
        company_edit(company)
    root = make_root(companies={slug: company})
    return load_config(root), root


class PlatformRouteTest(unittest.TestCase):
    def test_youtube_channel_id_uses_channel_route_and_handle_uses_user_route(self):
        cfg, _ = cfg_with()
        _, ch = rsshub.build_user_feed(cfg, "youtube", "UC1AyZZk4om839jYxD-yi05w", [], [], "https://hub.example", None)
        self.assertIn("/youtube/channel/UC1AyZZk4om839jYxD-yi05w", ch)
        _, us = rsshub.build_user_feed(cfg, "youtube", "gekkou_official", [], [], "https://hub.example", None)
        self.assertIn("/youtube/user/@gekkou_official", us)
        _, ig = rsshub.build_user_feed(cfg, "instagram", "sciencesaru", [], [], "https://hub.example", None)
        self.assertIn("/instagram/user/sciencesaru", ig)
        _, bs = rsshub.build_user_feed(cfg, "bluesky", "someone.bsky.social", [], [], "https://hub.example", None)
        self.assertIn("/bsky/posts/someone.bsky.social", bs)


class RoutingTest(unittest.TestCase):
    def test_keyword_boundaries(self):
        self.assertTrue(keyword_in_text("TA", "TA募集"))
        self.assertTrue(keyword_in_text("TA", "募集 (TA) です"))
        self.assertFalse(keyword_in_text("TA", "data entry"))
        self.assertFalse(keyword_in_text("TA", "STAFF"))
        self.assertTrue(keyword_in_text("3DCG", "3DCGアーティスト募集"))
        self.assertTrue(keyword_in_text("原画", "動画と原画を募集"))
        self.assertFalse(keyword_in_text("", "x"))

    def test_pick_roles_narrows_by_text_and_exclude_words(self):
        cfg, _ = cfg_with()
        company = cfg.company("cloverworks")
        self.assertEqual(pick_roles(cfg, company, text="原画マンを募集します"), ["key_animator"])
        # 原画の除外語(原画展)を含む投稿は、原画には送らない。職種が分からなければ職種なし(→ fallback)
        self.assertEqual(pick_roles(cfg, company, text="原画展を開催します"), [])
        self.assertEqual(pick_roles(cfg, company, text="なんの関係もない本文"), [])
        self.assertEqual(pick_roles(cfg, company), [])
        self.assertEqual(pick_roles(cfg, company, text="制作進行・原画を募集"), ["production_assistant", "key_animator"])
        # 会社の roles に書いていない職種でも、本文に出てくれば送る
        self.assertIn("background_art", pick_roles(cfg, {**company, "roles": []}, text="美術を募集"))
        self.assertEqual(pick_roles(cfg, company, hint_roles=["finishing", "compositing"]), ["finishing", "compositing"])
        self.assertEqual(pick_roles(cfg, company, hint_roles=["finishing", "key_animator"], text="原画を募集"), ["key_animator"])

    def test_resolve_channels_dedupes_and_falls_back(self):
        channels = {"roles": {"a": "111", "b": "111", "c": "222"}, "fallback": "999", "errors": None}
        targets = resolve_channels(["a", "b", "c", "x"], channels)
        by_id = {t.channel_id: t for t in targets}
        self.assertEqual(set(by_id), {"111", "222", "999"})
        self.assertEqual(by_id["111"].roles, ["a", "b"])
        self.assertTrue(by_id["999"].is_fallback)
        self.assertEqual(resolve_channels(["x"], {"roles": {}, "fallback": None}), [])
        # 種別(type)のチャンネル: 職種なし・未割り当ての通知は、種別のチャンネル → fallback の順
        ch = {"roles": {"a": "111"}, "fallback": "999", "types": {"portal": "555"}}
        self.assertEqual([t.channel_id for t in resolve_channels([], ch, "portal")], ["555"])
        self.assertEqual([t.channel_id for t in resolve_channels(["x"], ch, "portal")], ["555"])
        self.assertEqual([t.channel_id for t in resolve_channels([], ch, "studio")], ["999"])   # studio は未設定 → fallback
        self.assertEqual([t.channel_id for t in resolve_channels(["a"], ch, "portal")], ["111"])  # 職種があればそちら
        self.assertEqual(resolve_channels(["a"], {"roles": {"a": "111"}, "fallback": "999"})[0].channel_id, "111")


class RsshubUrlTest(unittest.TestCase):
    def test_user_feed_url(self):
        cfg, _ = cfg_with()
        feeds, errors = rsshub.build_feeds(cfg, "https://hub.example/", "KEY123")
        self.assertEqual(errors, [])
        self.assertEqual([f.key for f in feeds], ["cloverworks:x:user:CloverWorks"])  # monitor: false のアカウントは対象外
        feed = feeds[0]
        parts = urlsplit(feed.url)
        self.assertEqual(parts.path, "/twitter/user/CloverWorks")
        q = parse_qs(parts.query)
        self.assertIn("採用", q["filter"][0].split("|"))
        self.assertEqual(q["filterout"][0].split("|"), ["抽選", "発売", "プレゼント", "🎁", "キャンペーン", "舞台挨拶", "お便り"])
        self.assertEqual(q["filter"][0].split("|"), ["募集", "応募", "採用", "求人", "新卒", "企業説明会", "会社説明会"])
        self.assertEqual(q["format"], ["json"])
        self.assertEqual(q["includeRts"], ["0"])                      # リポストを含めない
        self.assertNotIn("KEY123", feed.url)
        self.assertIn("key=KEY123", feed.request_url)
        self.assertNotIn("KEY123", rsshub.mask_key(feed.request_url))

    def test_retweet_switch_off_removes_param(self):
        cfg, _ = cfg_with()
        cfg.common["sns"]["exclude_retweets"] = False
        feeds, _ = rsshub.build_feeds(cfg, "https://hub.example")
        self.assertNotIn("includeRts", feeds[0].url)

    def test_is_retweet(self):
        for text in ("RT @a: 採用", "  RT @a: x", "🔁 a: 採用"):
            self.assertTrue(rsshub.is_retweet(text), text)
        for text in ("採用のお知らせ RT @a 募集", "", "RTは含まない", None):
            self.assertFalse(rsshub.is_retweet(text), text)

    def test_handle_with_at_sign_and_youtube_prefix(self):
        self.assertEqual(normalize_handle(" @Name "), "Name")
        self.assertIsNone(normalize_handle(None))
        self.assertIsNone(normalize_handle("@"))

        def edit(c):
            c["sns"]["accounts"]["x"] = [{"handle": "@CloverWorks", "monitor": True}]
            c["sns"]["accounts"]["youtube"] = [{"handle": "clover", "monitor": True}]
        cfg, _ = cfg_with(edit)
        feeds, _ = rsshub.build_feeds(cfg, "https://hub.example", None)
        urls = {f.platform: f.url for f in feeds}
        self.assertTrue(urls["x"].startswith("https://hub.example/twitter/user/CloverWorks?"))
        self.assertEqual(urls["youtube"], "https://www.youtube.com/@clover")   # YouTube は RSSHub を通さず直接読む(common.yaml の direct)

    def test_filter_add_remove(self):
        def edit(c):
            c["sns"]["search"]["default_filter"]["include"] = {"add": ["動画研修生"], "remove": ["求人"]}
            c["sns"]["search"]["default_filter"]["exclude"] = {"add": ["配信"], "remove": ["プレゼント"]}
        cfg, _ = cfg_with(edit)
        feeds, _ = rsshub.build_feeds(cfg, "https://hub.example")
        q = parse_qs(urlsplit(feeds[0].url).query)
        inc, exc = q["filter"][0].split("|"), q["filterout"][0].split("|")
        self.assertIn("動画研修生", inc)
        self.assertNotIn("求人", inc)
        self.assertIn("配信", exc)
        self.assertNotIn("プレゼント", exc)

    def test_no_exclude_means_no_filterout(self):
        def edit(c):
            c["sns"]["search"]["default_filter"]["exclude"] = {"remove": ["抽選", "発売", "プレゼント", "🎁", "キャンペーン", "舞台挨拶", "お便り"]}
        cfg, _ = cfg_with(edit)
        feeds, _ = rsshub.build_feeds(cfg, "https://hub.example")
        self.assertNotIn("filterout", feeds[0].url)

    def test_search_presets_chunked_within_limit(self):
        def edit(c):
            c["roles"] = [r for r in load_config(make_root()).job_roles]  # 全職種
            c["sns"]["search"]["use_presets"] = ["company_recruit", "role_recruit"]
        cfg, _ = cfg_with(edit)
        company = cfg.company("cloverworks")
        limit = cfg.sns["search_max_query_length"]
        queries = rsshub.expand_query(cfg, company, cfg.sns["search_presets"]["role_recruit"]["x"]["query"])
        self.assertGreater(len(queries), 1)
        for q, roles in queries:
            self.assertLessEqual(len(q), limit)
            self.assertTrue(q.endswith("-filter:retweets"))
            self.assertIn("(CloverWorks OR クローバーワークス)", q)
        covered = [r for _, roles in queries for r in roles]
        self.assertEqual(sorted(covered), sorted(set(covered)))        # 重複なし
        self.assertEqual(set(covered), set(company["roles"]))           # 全職種を網羅
        feeds, errors = rsshub.build_feeds(cfg, "https://hub.example")
        self.assertEqual(errors, [])
        search = [f for f in feeds if f.kind == "search"]
        self.assertEqual(len(search), len(queries) + 1)
        self.assertTrue(all(urlsplit(f.url).path.startswith("/twitter/keyword/") for f in search))
        decoded = unquote(urlsplit(search[0].url).path.split("/twitter/keyword/")[1])
        self.assertIn("OR", decoded)

    def test_query_too_long_is_reported(self):
        def edit(c):
            c["sns"]["search"]["keywords"] = ["あ" * 300, "い" * 300]
            c["sns"]["search"]["use_presets"] = ["company_recruit"]
        cfg, _ = cfg_with(edit)
        feeds, errors = rsshub.build_feeds(cfg, "https://hub.example")
        self.assertTrue(any("長すぎ" in reason for _, reason in errors))

    def test_custom_query(self):
        def edit(c):
            c["sns"]["search"]["custom"] = {"mine": {"platform": "x", "query": "(CloverWorks) 採用 -BONDS"}}
        cfg, _ = cfg_with(edit)
        feeds, errors = rsshub.build_feeds(cfg, "https://hub.example")
        self.assertEqual(errors, [])
        self.assertTrue(any(f.key.endswith("search:custom:mine") for f in feeds))

    def test_unknown_preset_and_platform_without_search_feed(self):
        def edit(c):
            c["sns"]["search"]["use_presets"] = ["nope"]
        cfg, _ = cfg_with(edit)
        _, errors = rsshub.build_feeds(cfg, "https://hub.example")
        self.assertTrue(errors)

    def test_only_filter(self):
        root = make_root(companies={"cloverworks": load_sample("cloverworks"), "bones": load_sample("bones")})
        cfg = load_config(root)
        self.assertEqual(len(rsshub.build_feeds(cfg, "https://h", only="bones")[0]), 0)
        feeds = rsshub.build_feeds(cfg, "https://h", only="cloverworks")[0]
        self.assertTrue(feeds and all(f.company_slug == "cloverworks" for f in feeds))


class RsshubFetchTest(unittest.TestCase):
    def setUp(self):
        self.hub = MockRSSHub()
        self.addCleanup(self.hub.close)

    def test_fetch_and_parse(self):
        self.hub.feeds["/twitter/user/A"] = [
            {"id": "1", "url": "https://x.com/A/status/1", "content_html": "<p>募集<br>します&amp;</p>", "date_published": "2026-10-29T00:00:00Z"},
            {"id": "2", "url": "https://x.com/A/status/2", "title": "タイトルだけ"},
            {"url": "https://x.com/A/status/3", "content_text": "idなし"},
            {"nothing": True},
        ]
        items = rsshub.fetch_feed(self.hub.base + "/twitter/user/A?format=json")
        self.assertEqual([i.id for i in items], ["1", "2", "https://x.com/A/status/3"])
        self.assertEqual(items[0].text, "募集\nします&")
        self.assertEqual(items[1].text, "タイトルだけ")

    def test_fetch_errors_do_not_leak_key(self):
        self.hub.require_key = "RIGHT"
        with self.assertRaises(rsshub.FeedFetchError) as ctx:
            rsshub.fetch_feed(self.hub.base + "/twitter/user/A?format=json&key=WRONGKEY")
        self.assertIn("403", str(ctx.exception))
        self.assertNotIn("WRONGKEY", str(ctx.exception))
        with self.assertRaises(rsshub.FeedFetchError):
            rsshub.fetch_feed(self.hub.base + "/missing")
        with self.assertRaises(rsshub.FeedFetchError):
            rsshub.fetch_feed("http://127.0.0.1:9/x", timeout=2)

    def test_non_json_response(self):
        import http.server
        # 404 以外で JSON でないもの: 文字列を返すフィードを作る
        self.hub.feeds["/bad"] = []
        class Opener:
            def __call__(self, request, timeout=0):
                import io
                class R(io.BytesIO):
                    def __enter__(s): return s
                    def __exit__(s, *a): return False
                return R(b"<html>not json</html>")
        with self.assertRaises(rsshub.FeedFetchError):
            rsshub.fetch_feed("http://x/bad", opener=Opener())


class StateTest(unittest.TestCase):
    def test_roundtrip_and_cap(self):
        import tempfile, pathlib
        path = pathlib.Path(tempfile.mkdtemp()) / "sub" / "state.json"
        s = State(path)
        self.assertFalse(s.initialized("k"))
        s.mark("k", [str(i) for i in range(700)])
        s.save()
        s2 = State(path)
        self.assertTrue(s2.initialized("k"))
        self.assertTrue(s2.seen("k", "699"))
        self.assertFalse(s2.seen("k", "0"))  # 古いものは落ちる
        self.assertEqual(len(s2.feeds["k"]), 500)

    def test_empty_feed_is_initialized_and_corrupt_file_is_tolerated(self):
        import tempfile, pathlib
        path = pathlib.Path(tempfile.mkdtemp()) / "state.json"
        s = State(path)
        s.mark("k", [])
        self.assertTrue(s.initialized("k"))
        path.write_text("{壊れた", encoding="utf-8")
        self.assertFalse(State(path).initialized("k"))


class ReportParseTest(unittest.TestCase):
    SAMPLE = """\
===========================================================================
01. CHANGED: 採用情報 - A ( https://a.example/ )
02. ERROR: 採用情報 - B ( https://b.example/ )
03. NEW: 採用情報 - C ( https://c.example/ )
===========================================================================


---------------------------------------------------------------------------
CHANGED: 採用情報 - A ( https://a.example/ )
---------------------------------------------------------------------------
--- @	x
+++ @	y
@@ -1 +1 @@
-古い
+新しい
CHANGED: 採用情報 - A ( https://a.example/ )

---------------------------------------------------------------------------
ERROR: 採用情報 - B ( https://b.example/ )
---------------------------------------------------------------------------
Error: Connection refused

---------------------------------------------------------------------------
NEW: 採用情報 - C ( https://c.example/ )
---------------------------------------------------------------------------

-- 
urlwatch 2.29, Copyright 2008-2023 Thomas Perl
"""

    def test_parse(self):
        entries = parse_report(self.SAMPLE)
        self.assertEqual([(e.status, e.name) for e in entries],
                         [("CHANGED", "採用情報 - A"), ("ERROR", "採用情報 - B"), ("NEW", "採用情報 - C")])
        self.assertIn("+新しい", entries[0].body)
        self.assertIn("CHANGED: 採用情報 - A ( https://a.example/ )", entries[0].body)  # 本文中の同形の行は見出しにならない
        self.assertEqual(entries[1].body, "Error: Connection refused")
        self.assertEqual(entries[2].body, "")  # フッターは本文に入らない
        self.assertEqual(entries[0].url, "https://a.example/")

    def test_known_names_filter_and_empty(self):
        self.assertEqual(len(parse_report(self.SAMPLE, {"採用情報 - A"})), 1)
        self.assertEqual(parse_report(""), [])
        self.assertEqual(parse_report("何も変更なし"), [])


if __name__ == "__main__":
    unittest.main()


class YoutubeDirectTest(unittest.TestCase):
    ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" xmlns:media="http://search.yahoo.com/mrss/" xmlns="http://www.w3.org/2005/Atom">
 <entry><id>yt:video:AAA</id><yt:videoId>AAA</yt:videoId><title>原画マン募集のお知らせ</title>
  <link rel="alternate" href="https://www.youtube.com/watch?v=AAA"/><published>2026-11-01T03:00:00+00:00</published>
  <media:group><media:description>応募はこちら</media:description></media:group></entry>
 <entry><id>yt:video:BBB</id><yt:videoId>BBB</yt:videoId><title>新作PV公開</title>
  <link rel="alternate" href="https://www.youtube.com/watch?v=BBB"/><published>2026-11-02T03:00:00+00:00</published>
  <media:group><media:description>PVです</media:description></media:group></entry>
 <entry><id>yt:video:CCC</id><yt:videoId>CCC</yt:videoId><title>グッズ抽選のお知らせ</title>
  <link rel="alternate" href="https://www.youtube.com/watch?v=CCC"/><published>2026-11-03T03:00:00+00:00</published>
  <media:group><media:description>採用ではありません 募集</media:description></media:group></entry>
</feed>"""

    class Resp:
        def __init__(self, text):
            self.text = text

        def read(self):
            return self.text.encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def make_feed(self, handle):
        cfg, _ = cfg_with()
        company = cfg.company("cloverworks")
        include, exclude = rsshub.effective_filter(cfg, company)
        p = cfg.sns["platforms"]["youtube"]
        shown, req = rsshub.build_direct_youtube(p, handle)
        return rsshub.Feed("k", "id", "cloverworks", "C", "youtube", "YouTube", "user", "L", shown, req,
                           direct="youtube", handle=handle, include=include, exclude=exclude)

    def test_channel_id_reads_official_feed_and_filters_locally(self):
        feed = self.make_feed("UC1AyZZk4om839jYxD-yi05w")
        self.assertEqual(feed.request_url, "https://www.youtube.com/feeds/videos.xml?channel_id=UC1AyZZk4om839jYxD-yi05w")
        seen = []
        def opener(req, timeout):
            seen.append(req.full_url)
            return self.Resp(self.ATOM)
        items = rsshub.fetch_youtube(feed, opener=opener)
        self.assertEqual([i.id for i in items], ["yt:video:AAA"])      # 採用ワードのある動画だけ。「抽選」は除外
        self.assertIn("応募はこちら", items[0].text)
        self.assertEqual(items[0].url, "https://www.youtube.com/watch?v=AAA")
        self.assertEqual(items[0].published, "2026-11-01T03:00:00+00:00")
        self.assertEqual(len(seen), 1)

    def test_handle_resolves_channel_id_once(self):
        feed = self.make_feed("gekkou_official")
        calls, resolved = [], {}
        page = '<html><link rel="alternate" type="application/rss+xml" href="https://www.youtube.com/feeds/videos.xml?channel_id=UC1AyZZk4om839jYxD-yi05w"></html>'
        def opener(req, timeout):
            calls.append(req.full_url)
            return self.Resp(page if "/@gekkou_official" in req.full_url else self.ATOM)
        rsshub.fetch_youtube(feed, resolved=resolved, opener=opener)
        self.assertEqual(resolved, {"gekkou_official": "UC1AyZZk4om839jYxD-yi05w"})
        rsshub.fetch_youtube(feed, resolved=resolved, opener=opener)
        self.assertEqual(sum("/@gekkou_official" in c for c in calls), 1)   # 2回目はページを取り直さない

    def test_errors_are_readable(self):
        feed = self.make_feed("nosuchchannel")
        with self.assertRaises(rsshub.FeedFetchError) as cm:
            rsshub.fetch_youtube(feed, opener=lambda req, timeout: self.Resp("<html>nothing</html>"))
        self.assertIn("チャンネルID", str(cm.exception))
        def boom(req, timeout):
            raise TimeoutError()
        with self.assertRaises(rsshub.FeedFetchError) as cm:
            rsshub.fetch_youtube(self.make_feed("UC1AyZZk4om839jYxD-yi05w"), opener=boom)
        self.assertEqual(str(cm.exception), "タイムアウト")


class YoutubeApiTest(unittest.TestCase):
    def make_feed(self, handle):
        cfg, _ = cfg_with()
        include, exclude = rsshub.effective_filter(cfg, cfg.company("cloverworks"))
        shown, req = rsshub.build_direct_youtube(cfg.sns["platforms"]["youtube"], handle)
        return rsshub.Feed("k", "id", "cloverworks", "C", "youtube", "YouTube", "user", "L", shown, req,
                           direct="youtube_api", handle=handle, include=include, exclude=exclude)

    def opener(self, calls):
        import json as _json
        class R:
            def __init__(self, d): self.d = d
            def read(self): return _json.dumps(self.d).encode()
            def __enter__(self): return self
            def __exit__(self, *a): return False
        def pl(vid, title, desc, date):
            return {"snippet": {"title": title, "description": desc, "publishedAt": date, "resourceId": {"videoId": vid}, "videoOwnerChannelTitle": "クローバーワークス公式"},
                    "contentDetails": {"videoId": vid, "videoPublishedAt": date}}
        def op(req, timeout):
            calls.append(req.full_url)
            if "/channels?" in req.full_url:
                return R({"items": [{"id": "UC1AyZZk4om839jYxD-yi05w"}]})
            return R({"items": [pl("AAA", "原画マン募集", "応募はこちら", "2026-11-01T03:00:00Z"),
                                pl("BBB", "新作PV", "PVです", "2026-11-02T03:00:00Z"),
                                pl("CCC", "Private video", "", "2026-11-03T03:00:00Z")]})
        return op

    def test_handle_resolved_once_and_filtered(self):
        feed, calls, resolved = self.make_feed("gekkou_official"), [], {}
        items = rsshub.fetch_youtube_api(feed, "KEY123", resolved=resolved, opener=self.opener(calls))
        self.assertEqual([i.id for i in items], ["yt:video:AAA"])
        self.assertEqual(items[0].published, "2026-11-01T03:00:00Z")
        self.assertEqual(feed.channel_title, "クローバーワークス公式")
        self.assertIn("forHandle=%40gekkou_official", calls[0])
        self.assertIn("playlistId=UU1AyZZk4om839jYxD-yi05w", calls[1])
        rsshub.fetch_youtube_api(feed, "KEY123", resolved=resolved, opener=self.opener(calls))
        self.assertEqual(sum("/channels?" in c for c in calls), 1)

    def test_channel_id_skips_lookup(self):
        calls = []
        rsshub.fetch_youtube_api(self.make_feed("UC1AyZZk4om839jYxD-yi05w"), "K", opener=self.opener(calls))
        self.assertEqual(len(calls), 1)

    def test_channel_without_videos_is_empty_not_error(self):
        import urllib.error
        def not_found(req, timeout):
            raise urllib.error.HTTPError(req.full_url, 404, "nf", {}, None)
        self.assertEqual(rsshub.fetch_youtube_api(self.make_feed("UC1AyZZk4om839jYxD-yi05w"), "K", opener=not_found), [])

    def test_missing_key_and_errors_do_not_leak_key(self):
        with self.assertRaises(rsshub.FeedFetchError) as cm:
            rsshub.fetch_youtube_api(self.make_feed("x"), None)
        self.assertIn("YOUTUBE_API_KEY", str(cm.exception))
        import urllib.error
        def forbidden(req, timeout):
            raise urllib.error.HTTPError(req.full_url, 403, "no", {}, None)
        with self.assertRaises(rsshub.FeedFetchError) as cm:
            rsshub.fetch_youtube_api(self.make_feed("UC1AyZZk4om839jYxD-yi05w"), "SECRETKEY", opener=forbidden)
        self.assertEqual(str(cm.exception), "HTTP 403")


class PlatformEnabledTest(unittest.TestCase):
    def test_disabled_platform_makes_no_feeds(self):
        def edit(c):
            c["sns"]["accounts"]["instagram"] = [{"handle": "clover", "monitor": True}]
        cfg, _ = cfg_with(edit)
        cfg.sns["platforms"]["instagram"]["enabled"] = False
        feeds, _ = rsshub.build_feeds(cfg, "https://hub.example", None)
        self.assertFalse([f for f in feeds if f.platform == "instagram"])
        cfg.sns["platforms"]["instagram"]["enabled"] = True
        feeds, _ = rsshub.build_feeds(cfg, "https://hub.example", None)
        self.assertTrue([f for f in feeds if f.platform == "instagram"])


class FetchAllTest(unittest.TestCase):
    """並行取得: 結果は渡した順で返り、失敗は他のフィードに影響せず、開始間隔は守られる。"""

    def _run(self, workers, interval=0.0):
        import threading, time
        from watchlib.runners import _fetch_all
        from watchlib.rsshub import FeedFetchError

        class F:
            def __init__(self, k): self.key = k
        feeds = [F(f"f{i}") for i in range(8)]
        starts, lock = [], threading.Lock()

        def fetch_one(feed):
            with lock:
                starts.append(time.monotonic())
            time.sleep(0.05)
            if feed.key == "f3":
                raise FeedFetchError("タイムアウト")
            return [feed.key]
        out = list(_fetch_all(feeds, fetch_one, workers, interval, time.sleep, time.monotonic))
        return feeds, out, starts

    def test_order_and_errors_kept(self):
        for workers in (1, 4):
            feeds, out, _ = self._run(workers)
            self.assertEqual([o[0].key for o in out], [f.key for f in feeds])
            self.assertEqual([o[2] is not None for o in out], [i == 3 for i in range(8)])
            self.assertEqual(out[0][1], ["f0"])

    def test_parallel_is_faster_and_interval_kept(self):
        import time
        t = time.monotonic(); self._run(1); serial = time.monotonic() - t
        t = time.monotonic(); self._run(4); parallel = time.monotonic() - t
        self.assertLess(parallel, serial * 0.6)
        _, _, starts = self._run(4, interval=0.1)
        gaps = [b - a for a, b in zip(sorted(starts), sorted(starts)[1:])]
        self.assertGreaterEqual(min(gaps), 0.09)


class _Bot:
    def __init__(self): self.sent = []
    def send_message(self, ch, content, **kw): self.sent.append((ch, content))


class RateLimitTest(unittest.TestCase):
    """X(RSSHub)の要求制限: ほぼ全滅したら待ち、続くなら残りを次回に回して、まとめて1件で知らせる。古いものから先に取る。"""

    def _feeds(self, n):
        from watchlib.rsshub import Feed
        return [Feed(key=f"c{i}:x:user:a{i}", company_id=str(i), company_slug=f"c{i}", company_name=f"c{i}", platform="x",
                     platform_label="X", kind="user", label="x", url="u", request_url=f"http://hub/{i}") for i in range(n)]

    def _run(self, fetch, feeds, state, **kw):
        from types import SimpleNamespace
        from watchlib.runners import run_rsshub
        cfg = SimpleNamespace(sns={"fetch": {"retry_rounds": 0, "concurrency": 2, "rate_limit": {"chunk_size": 4, "wait_seconds": 5, "waits": 1}}},
                              discord_channels={"errors": "E"}, company=lambda s: {})
        sleeps, logs = [], []
        res = run_rsshub(cfg, feeds, state, _Bot(), dry_run=True, fetch=fetch, request_interval=0, error_renotify_hours=0,
                         sleep=sleeps.append, log=logs.append, **kw)
        return res, sleeps, logs

    def test_limited_run_waits_then_defers_rest(self):
        from watchlib.rsshub import FeedFetchError
        from watchlib.state import State
        calls = []

        def fetch(url):
            calls.append(url)
            raise FeedFetchError("タイムアウト")
        res, sleeps, logs = self._run(fetch, self._feeds(12), State(None))
        self.assertEqual(sleeps, [5])                                   # 1回だけ待つ
        self.assertTrue(any("次回に回します" in l for l in logs))
        self.assertLess(len(calls), 12 * 2)                             # 全部を何度も叩かない
        self.assertEqual(len([e for e in res.errors if "要求制限" in e[0]]), 1)   # まとめて1件で知らせる

    def test_oldest_fetched_first(self):
        from watchlib.state import State
        st = State(None)
        feeds = self._feeds(5)
        for i, f in enumerate(feeds):
            st.fetched[f.key] = 1000.0 - i     # c4 がいちばん古い
        order = []

        def fetch(url):
            order.append(url)
            return []
        self._run(fetch, feeds, st)
        self.assertEqual(order[0], "http://hub/4")
