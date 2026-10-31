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
    if company_edit:
        company_edit(company)
    root = make_root(companies={slug: company})
    return load_config(root), root


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
        # 原画の除外語(原画展)を含む投稿は、原画には送らない → 絞れないので全職種
        all_roles = pick_roles(cfg, company)
        self.assertEqual(pick_roles(cfg, company, text="原画展を開催します"), all_roles)
        self.assertEqual(pick_roles(cfg, company, text="なんの関係もない本文"), all_roles)
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
        self.assertIn("質問募集", q["filterout"][0].split("|"))
        self.assertEqual(q["format"], ["json"])
        self.assertNotIn("KEY123", feed.url)
        self.assertIn("key=KEY123", feed.request_url)
        self.assertNotIn("KEY123", rsshub.mask_key(feed.request_url))

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
        self.assertTrue(urls["youtube"].startswith("https://hub.example/youtube/user/@clover?"))

    def test_filter_add_remove(self):
        def edit(c):
            c["sns"]["search"]["default_filter"]["include"] = {"add": ["動画研修生"], "remove": ["中途"]}
            c["sns"]["search"]["default_filter"]["exclude"] = {"add": ["配信"], "remove": ["プレゼント"]}
        cfg, _ = cfg_with(edit)
        feeds, _ = rsshub.build_feeds(cfg, "https://hub.example")
        q = parse_qs(urlsplit(feeds[0].url).query)
        inc, exc = q["filter"][0].split("|"), q["filterout"][0].split("|")
        self.assertIn("動画研修生", inc)
        self.assertNotIn("中途", inc)
        self.assertIn("配信", exc)
        self.assertNotIn("プレゼント", exc)

    def test_no_exclude_means_no_filterout(self):
        def edit(c):
            c["sns"]["search"]["default_filter"]["exclude"] = {"remove": ["プレゼント", "キャンペーン", "質問募集", "感想募集", "イラスト募集", "投稿募集"]}
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
        self.assertEqual(len(rsshub.build_feeds(cfg, "https://h", only="cloverworks")[0]), 1)


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
