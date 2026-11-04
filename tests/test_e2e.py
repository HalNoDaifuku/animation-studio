import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

import helpers
from helpers import MockDiscord, MockRSSHub, MockWeb, load_sample, make_root, write_company
import discord_setup_channels
import check_companies
import rsshub_notify
import urlwatch_notify

CH_A, CH_B, CH_FB, CH_ERR = "100000000000000001", "100000000000000002", "100000000000000003", "100000000000000004"
URLWATCH = os.environ.get("URLWATCH_CMD") or shutil.which("urlwatch") or ("/tmp/uwenv/bin/urlwatch" if Path("/tmp/uwenv/bin/urlwatch").exists() else None)


def run(main, *args, env=None):
    old = dict(os.environ)
    # 実行環境(GitHub Actions の Secrets など)の値が混ざらないよう、関係する環境変数を先に消す
    for key in ("DISCORD_BOT_TOKEN", "DISCORD_GUILD_ID", "RSSHUB_BASE_URL", "RSSHUB_ACCESS_KEY", "URLWATCH_CMD"):
        os.environ.pop(key, None)
    os.environ.update(env or {})
    try:
        return main(list(args))
    finally:
        os.environ.clear()
        os.environ.update(old)


class RsshubE2E(unittest.TestCase):
    def setUp(self):
        self.discord, self.hub = MockDiscord(), MockRSSHub()
        self.addCleanup(self.discord.close)
        self.addCleanup(self.hub.close)
        self.root = make_root(self.discord.base, roles_to_channels={"key_animator": CH_A, "in_between": CH_B},
                              fallback=CH_FB, errors=CH_ERR, companies={"cloverworks": load_sample("cloverworks")})
        self.env = {"DISCORD_BOT_TOKEN": "TOK", "RSSHUB_BASE_URL": self.hub.base}
        self.state = self.root / "state.json"
        self.path = "/twitter/user/CloverWorks"

    def go(self, *extra):
        return run(rsshub_notify.main, "--root", str(self.root), "--state", str(self.state), "--retry-wait", "0", "--request-interval", "0", *extra, env=self.env)

    def item(self, i, text):
        return {"id": str(i), "url": f"https://x.com/CloverWorks/status/{i}", "content_text": text, "date_published": "2026-10-29T12:00:00Z"}

    def test_baseline_then_notify_once_and_route_by_role(self):
        self.hub.feeds[self.path] = [self.item(1, "既存の採用投稿")]
        self.assertEqual(self.go(), 0)
        self.assertEqual(self.discord.messages(), [])            # 初回は通知しない
        self.hub.feeds[self.path] = [self.item(3, "動画の募集を開始"), self.item(2, "原画マン募集"), self.item(1, "既存の採用投稿")]
        self.assertEqual(self.go(), 0)
        sent = self.discord.messages()
        self.assertEqual([(m["channel"], "原画" in m["content"]) for m in sent[:1]], [(CH_A, True)])   # 古い順: 原画 → 原画チャンネル
        self.assertEqual(sent[1]["channel"], CH_B)                                                      # 動画 → in_between チャンネル
        self.assertEqual(len(sent), 2)
        self.assertEqual(sent[0]["headers"]["Authorization"], "Bot TOK")
        self.go()
        self.assertEqual(len(self.discord.messages()), 2)        # 同じ投稿は再通知しない

    def test_unmatched_text_goes_only_to_fallback(self):
        self.hub.feeds[self.path] = []
        self.go()
        self.hub.feeds[self.path] = [self.item(1, "採用に関するお知らせ")]
        self.go()
        channels = sorted(m["channel"] for m in self.discord.messages())
        self.assertEqual(channels, [CH_FB])                       # 職種が分からない通知は、全職種ではなく fallback だけ

    def test_text_goes_only_to_matching_roles(self):
        self.hub.feeds[self.path] = []
        self.go()
        self.hub.feeds[self.path] = [self.item(1, "原画マンを募集します")]
        self.go()
        self.assertEqual([m["channel"] for m in self.discord.messages()], [CH_A])   # 原画の channel だけ(動画の CH_B や fallback には送らない)
        self.discord.requests.clear()
        self.hub.feeds[self.path] = [self.item(2, "制作進行を募集します"), self.item(1, "原画マンを募集します")]
        self.go()
        self.assertEqual([m["channel"] for m in self.discord.messages()], [CH_FB])  # 制作進行はチャンネル未割り当て → fallback

    def test_send_failure_keeps_item_unseen_and_exit_1(self):
        self.hub.feeds[self.path] = []
        self.go()
        self.hub.feeds[self.path] = [self.item(1, "原画マン募集")]
        self.discord.fail[CH_A] = [400]
        self.assertEqual(self.go(), 1)
        self.assertEqual(self.go(), 0)                            # 次回、再送される
        self.assertEqual(len(self.discord.messages(CH_A)), 2)       # 失敗した1回 + 再送の1回
        self.go()
        self.assertEqual(len(self.discord.messages(CH_A)), 2)

    def test_limit_and_error_report(self):
        self.hub.feeds[self.path] = []
        self.go()
        self.hub.feeds[self.path] = [self.item(i, "原画募集") for i in range(10, 0, -1)]
        self.go("--limit", "3")
        self.assertEqual(len(self.discord.messages(CH_A)), 3)
        self.go("--limit", "3")
        self.assertEqual(len(self.discord.messages(CH_A)), 3)     # 省略分は既読になり、洪水にならない

    def test_retweets_are_not_notified(self):
        self.hub.feeds[self.path] = []
        self.go()
        self.hub.feeds[self.path] = [self.item(2, "RT @other: 原画マン募集"), self.item(1, "原画マン募集")]
        self.go()
        self.assertEqual(len(self.discord.messages(CH_A)), 1)
        self.assertNotIn("RT @", self.discord.messages(CH_A)[0]["content"])
        self.go()
        self.assertEqual(len(self.discord.messages(CH_A)), 1)    # RTは既読扱いで、再判定・再通知されない

    def test_no_filter_smoke_test_sends_latest_posts(self):
        self.hub.feeds[self.path] = [self.item(2, "新作のお知らせ"), self.item(1, "原画マン募集")]
        self.assertEqual(self.go("--no-filter", "--notify-on-first-run", "--limit", "2"), 0)
        contents = [m["content"] for m in self.discord.messages()]
        self.assertTrue(any("新作のお知らせ" in c for c in contents))       # 採用ワードの無い投稿も届く
        self.assertEqual(sum("原画マン募集" in c for c in contents), 1)      # 職種が分かる投稿は、その職種のチャンネルだけ
        self.assertTrue(all("filter=" not in p for p in self.hub.requested))

    def test_feed_error_goes_to_errors_channel_without_failing(self):
        # フィードが無い(404) → 取得エラー
        self.assertEqual(self.go(), 0)
        errs = self.discord.messages(CH_ERR)
        self.assertEqual(len(errs), 1)
        self.assertIn("RSSHub取得エラー", errs[0]["content"])

    def test_failed_feed_is_retried_after_all_feeds_and_recovers(self):
        self.hub.feeds[self.path] = [self.item(1, "既存")]
        self.assertEqual(self.go(), 0)                            # 初回(既読にするだけ)
        self.hub.feeds[self.path] = [self.item(2, "原画マン募集"), self.item(1, "既存")]
        self.hub.fail_first[self.path] = 2                        # 最初の2回は失敗、3回目(再試行2回目)で成功
        self.hub.requested.clear()
        self.assertEqual(self.go(), 0)
        self.assertEqual(len(self.hub.requested), 3)
        self.assertEqual(self.discord.messages(CH_ERR), [])       # 再試行で取れたので、エラー通知なし
        self.assertEqual(len(self.discord.messages(CH_A)), 1)     # 新着は通常どおり通知される

    def test_retry_gives_up_and_same_error_is_not_repeated_within_a_day(self):
        self.assertEqual(self.go(), 0)                            # 404 のまま
        self.assertEqual(len(self.hub.requested), 3)              # 1回 + 再試行2回
        self.assertEqual(len(self.discord.messages(CH_ERR)), 1)
        self.assertEqual(self.go(), 0)                            # 直後にもう一度
        self.assertEqual(len(self.discord.messages(CH_ERR)), 1)   # 同じエラーは、24時間は繰り返さない

    def test_access_key_sent_but_never_printed(self):
        self.hub.require_key = "SECRETKEY"
        self.hub.feeds[self.path] = [self.item(1, "x")]
        self.env["RSSHUB_ACCESS_KEY"] = "SECRETKEY"
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.go("--notify-on-first-run")
            self.go("--list")
        self.assertTrue(any("key=SECRETKEY" in p for p in self.hub.requested))
        self.assertNotIn("SECRETKEY", buf.getvalue())
        self.assertTrue(self.discord.messages())

    def test_dry_run_sends_nothing_and_saves_nothing(self):
        self.hub.feeds[self.path] = [self.item(1, "x")]
        self.assertEqual(self.go("--dry-run", "--notify-on-first-run"), 0)
        self.assertEqual(self.discord.requests, [])
        self.assertFalse(self.state.exists())

    def test_config_error_and_missing_token(self):
        self.env.pop("DISCORD_BOT_TOKEN")
        self.assertEqual(self.go(), 2)
        write_company(self.root, "broken", {"id": "x"})
        self.env["DISCORD_BOT_TOKEN"] = "TOK"
        self.assertEqual(self.go(), 2)


@unittest.skipUnless(URLWATCH, "urlwatch が無いのでスキップ(pip install urlwatch)")
class UrlwatchE2E(unittest.TestCase):
    def setUp(self):
        self.discord, self.web = MockDiscord(), MockWeb({"/recruit/": "<html><body><p>制作進行を募集</p><script>var nonce='a1'</script></body></html>"})
        self.addCleanup(self.discord.close)
        self.addCleanup(self.web.close)
        company = load_sample("cloverworks")
        company["monitors"][0]["url"] = self.web.base + "/recruit/"
        company["monitors"].append({"id": "gone", "name": "消えるページ", "url": self.web.base + "/missing/", "category": "news", "enabled": True,
                                    "roles": ["finishing"], "snippets": [], "skip_common_excludes": [], "urlwatch": {"max_tries": 1}})
        self.root = make_root(self.discord.base, roles_to_channels={"key_animator": CH_A, "production_assistant": CH_B, "finishing": CH_A},
                              fallback=CH_FB, errors=CH_ERR, companies={"cloverworks": company})
        self.env = {"DISCORD_BOT_TOKEN": "TOK"}

    def go(self, *extra):
        return run(urlwatch_notify.main, "--root", str(self.root), "--urlwatch-cmd", URLWATCH, "--retry-wait", "0", *extra, env=self.env)

    def test_full_cycle(self):
        self.assertEqual(self.go("--skip-new"), 0)
        self.assertEqual([m for m in self.discord.messages() if "エラー" not in m["content"]], [])   # NEW は送らない
        errs = self.discord.messages(CH_ERR)
        self.assertEqual(len(errs), 1)                              # 404 は errors チャンネルへ
        self.assertIn("消えるページ - CloverWorks", errs[0]["content"])
        self.discord.requests.clear()

        self.go("--skip-new")                                       # 変更なし → 通知なし(エラーは継続して出る)
        self.assertEqual([m for m in self.discord.messages() if m["channel"] != CH_ERR], [])
        self.discord.requests.clear()

        self.web.pages["/recruit/"] = "<html><body><p>制作進行を募集</p><script>var nonce='ZZZ'</script></body></html>"
        self.go("--skip-new")                                       # script だけの変更(nonce) → 誤検知しない
        self.assertEqual([m for m in self.discord.messages() if m["channel"] != CH_ERR], [])

        self.web.pages["/recruit/"] = "<html><body><p>制作進行を募集</p><p>原画を募集</p></body></html>"
        self.assertEqual(self.go("--skip-new"), 0)
        changed = [m for m in self.discord.messages() if "変更検知" in m["content"]]
        self.assertTrue(changed)
        self.assertTrue(all("```diff" in m["content"] and len(m["content"]) <= 2000 for m in changed))
        self.assertTrue(all(m["flags"] == 4 for m in changed))
        self.assertEqual({m["channel"] for m in changed}, {CH_A, CH_B})          # 増えた行にある職種(原画・制作進行)だけ。他の職種や fallback には送らない

    def test_error_is_retried_then_not_repeated_within_a_day(self):
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(self.go("--skip-new"), 0)
        self.assertIn("[再試行 1/1]", buf.getvalue())              # エラーのジョブだけ、全部終わってから取り直す
        self.assertEqual(len(self.discord.messages(CH_ERR)), 1)
        self.go("--skip-new")
        self.assertEqual(len(self.discord.messages(CH_ERR)), 1)    # 同じエラーは24時間は繰り返さない
        self.go("--skip-new", "--retry-rounds", "0")
        self.assertEqual(len(self.discord.messages(CH_ERR)), 1)

    def test_dry_run_does_not_advance_cache(self):
        self.go("--skip-new")
        self.web.pages["/recruit/"] = "<html><body><p>内容が変わった</p></body></html>"
        self.discord.requests.clear()
        self.go("--dry-run")
        self.assertEqual(self.discord.requests, [])
        self.go("--skip-new")                                       # dry-run のあとでも、本番でちゃんと検知できる
        self.assertTrue([m for m in self.discord.messages() if "変更検知" in m["content"]])

    def test_report_file_and_generate_only(self):
        report = self.root / "r.txt"
        report.write_text("=" * 75 + "\n01. CHANGED: 採用情報 - CloverWorks ( http://x/ )\n" + "=" * 75 + "\n\n" + "-" * 75 +
                          "\nCHANGED: 採用情報 - CloverWorks ( http://x/ )\n" + "-" * 75 + "\n-古い\n+新しい\n", encoding="utf-8")
        self.assertEqual(self.go("--report", str(report)), 0)
        self.assertTrue(any("+新しい" in m["content"] for m in self.discord.messages()))
        self.assertEqual(self.go("--generate-only"), 0)
        self.assertTrue((self.root / "build" / "urls.yaml").exists())

    def test_summary_lists_jobs_missing_from_report(self):
        import io, contextlib
        report = self.root / "r.txt"
        report.write_text("-" * 75 + "\nNEW: 採用情報 - CloverWorks ( http://x/ )\n" + "-" * 75 + "\n", encoding="utf-8")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.go("--report", str(report), "--skip-new", "--dry-run")
        out = buf.getvalue()
        self.assertIn("NEW 1件", out)
        self.assertIn("[報告なし] 消えるページ - CloverWorks", out)
        self.assertIn("今のURLの状態: HTTP 404", out)   # 原因調べとして、実際のURLを1回取得する

    def test_command_job_and_job_name_override(self):
        company = load_sample("cloverworks")
        company["slug"] = "cmdco"
        company["id"] = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        company["display_name"] = "コマンド社"
        last = self.root / "last-good.txt"
        company["monitors"] = [{"id": "recruit", "name": "採用情報", "job_name": "元の名前のまま", "category": "recruit",
                                "url": self.web.base + "/recruit/", "enabled": True, "roles": [], "snippets": [],
                                "skip_common_excludes": ["all"],
                                "command": f"{sys.executable} scripts/fetch_urlwatch_last_good.py --url {self.web.base}/recruit/ "
                                           f"--last-good {last} --retries 1 --sleep 0 --timeout 10"}]
        write_company(self.root, "cmdco", company)
        for stem in ("cloverworks",):
            (self.root / "db" / "companies" / f"{stem}.yaml").unlink()
        cwd = os.getcwd()
        os.chdir(helpers.REPO)  # command は、リポジトリのルートで実行される
        try:
            self.assertEqual(self.go("--skip-new"), 0)
            self.web.pages["/recruit/"] = "<html><body><p>制作進行・原画を募集</p></body></html>"
            self.assertEqual(self.go("--skip-new"), 0)
        finally:
            os.chdir(cwd)
        changed = [m for m in self.discord.messages() if "変更検知: 元の名前のまま" in m["content"]]
        self.assertTrue(changed)
        self.assertIn(f"URL: {self.web.base}/recruit/", changed[0]["content"])   # コマンドではなく、表示用のURL
        self.assertNotIn("fetch_urlwatch_last_good", changed[0]["content"])

    def test_portal_goes_to_type_channel(self):
        import yaml
        portal = load_sample("cloverworks")
        portal.update(slug="jobsite", id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", display_name="就活サイト", roles=[])
        portal["monitors"][0].update(url=self.web.base + "/recruit/", roles=[])
        write_company(self.root, "jobsite", portal, folder="portals")   # portals/ に置けば、type は自動で portal
        (self.root / "db" / "companies" / "cloverworks.yaml").unlink()
        path = self.root / "config" / "discord.yaml"
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        data["discord"]["channels"]["types"] = {"portal": "100000000000000009"}
        path.write_text(yaml.safe_dump(data), encoding="utf-8")
        self.go("--skip-new")
        self.web.pages["/recruit/"] = "<html><body><p>新着の求人</p></body></html>"
        self.go("--skip-new")
        changed = [m for m in self.discord.messages() if "変更検知" in m["content"]]
        self.assertEqual([m["channel"] for m in changed], ["100000000000000009"])   # 職種のチャンネルでも fallback でもない

    def test_missing_filter_module_stops_before_running(self):
        from watchlib.urlwatch_jobs import missing_filter_modules
        jobs = [{"name": "A", "filter": ["beautify", {"re.sub": {"pattern": "a"}}]}, {"name": "B", "filter": ["strip"]}]
        self.assertEqual(missing_filter_modules(jobs, find_spec=lambda m: None), {"bs4": ["A"]})
        self.assertEqual(missing_filter_modules(jobs, find_spec=lambda m: object()), {})

    def test_wayback_failure_does_not_block(self):
        import watchlib.runners as runners
        report = self.root / "r.txt"
        report.write_text("-" * 75 + "\nCHANGED: 採用情報 - CloverWorks ( http://x/ )\n" + "-" * 75 + "\n+新しい\n", encoding="utf-8")
        orig = runners.wayback_mod.capture
        runners.wayback_mod.capture = lambda url: ("", "HTTP 429")
        try:
            self.assertEqual(self.go("--report", str(report), "--wayback"), 0)
        finally:
            runners.wayback_mod.capture = orig
        self.assertTrue(any("保存失敗(HTTP 429)" in m["content"] for m in self.discord.messages()))


class SetupChannelsE2E(unittest.TestCase):
    def test_plan_apply_reuse(self):
        discord = MockDiscord()
        self.addCleanup(discord.close)
        root = make_root(discord.base, companies={"cloverworks": load_sample("cloverworks")})
        env = {"DISCORD_BOT_TOKEN": "TOK", "DISCORD_GUILD_ID": "777"}
        self.assertEqual(run(discord_setup_channels.main, "--root", str(root), env=env), 0)
        self.assertEqual([r for r in discord.requests if r["method"] == "POST"], [])   # 計画だけ
        self.assertEqual(run(discord_setup_channels.main, "--root", str(root), "--apply", env=env), 0)
        posts = [r["json"] for r in discord.requests if r["method"] == "POST"]
        self.assertEqual(posts[0]["type"], 4)                                           # カテゴリ
        names = [p["name"] for p in posts if p["type"] == 0]
        self.assertEqual(len(names), 2 + 33)                                            # fallback+errors+全33職種(本文から全職種に振り分けるため)
        self.assertIn("key-animator", names)
        self.assertTrue(all("parent_id" in p for p in posts if p["type"] == 0))
        before = len(posts)
        run(discord_setup_channels.main, "--root", str(root), "--apply", env=env)       # 2回目は再利用
        self.assertEqual(len([r for r in discord.requests if r["method"] == "POST"]), before)

    def test_over_50_channels_split_categories(self):
        from watchlib.channels import plan_channels
        from watchlib.config import load_config
        cfg = load_config(make_root())
        fake = {f"r{i}": {"name": f"r{i}"} for i in range(60)}
        cfg.common["job_roles"] = fake
        plan = plan_channels(cfg, [], "カテゴリ", list(fake))
        self.assertEqual(plan[0].category, "カテゴリ")
        self.assertEqual(plan[-1].category, "カテゴリ (2)")


class CheckScript(unittest.TestCase):
    def test_exit_codes(self):
        root = make_root(companies={"cloverworks": load_sample("cloverworks")})
        self.assertEqual(check_companies.main(["--root", str(root)]), 0)
        self.assertEqual(check_companies.main(["--root", str(root), "--strict"]), 1)   # 警告(未割り当て)で失敗
        c = load_sample("cloverworks"); c["id"] = "bad"
        write_company(root, "cloverworks", c)
        self.assertEqual(check_companies.main(["--root", str(root)]), 1)


if __name__ == "__main__":
    unittest.main()
