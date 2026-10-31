import unittest

import helpers  # noqa: F401  (sys.path の設定)
from helpers import MockDiscord
from watchlib.discord import USER_AGENT, DiscordBot, DiscordError, DryRunBot
from watchlib.messages import (DISCORD_LIMIT, build_diff_message, build_error_message, build_feed_error_message,
                               build_sns_message, format_jst, split_long_text)

TOKEN = "SECRET-TOKEN-123"


class DiscordBotTest(unittest.TestCase):
    def setUp(self):
        self.mock = MockDiscord()
        self.addCleanup(self.mock.close)
        self.sleeps: list[float] = []
        self.bot = DiscordBot(TOKEN, self.mock.base, sleep=self.sleeps.append)

    def test_headers_and_body(self):
        self.bot.send_message("123456789012345678", "こんにちは", suppress_embeds=True)
        req = self.mock.requests[0]
        self.assertEqual(req["headers"]["Authorization"], f"Bot {TOKEN}")
        self.assertEqual(req["headers"]["User-Agent"], USER_AGENT)
        self.assertTrue(USER_AGENT.startswith("DiscordBot ("))
        self.assertEqual(req["json"]["content"], "こんにちは")
        self.assertEqual(req["json"]["allowed_mentions"], {"parse": []})
        self.assertEqual(req["json"]["flags"], 4)

    def test_long_message_is_truncated_to_2000(self):
        self.bot.send_message("123456789012345678", "あ" * 5000)
        self.assertLessEqual(len(self.mock.requests[0]["json"]["content"]), DISCORD_LIMIT)

    def test_429_retry_uses_retry_after(self):
        self.mock.fail["123456789012345678"] = [429, 429]
        self.bot.send_message("123456789012345678", "x")
        self.assertEqual(len(self.mock.messages()), 3)  # 失敗2回 + 成功
        self.assertEqual(len([s for s in self.sleeps if s >= 0.5]), 2)

    def test_5xx_retry_then_success(self):
        self.mock.fail["123456789012345678"] = [502]
        self.bot.send_message("123456789012345678", "x")
        self.assertEqual(len(self.mock.messages()), 2)

    def test_4xx_raises_without_retry_and_hides_token(self):
        self.mock.fail["123456789012345678"] = [400]
        with self.assertRaises(DiscordError) as ctx:
            self.bot.send_message("123456789012345678", "x")
        self.assertEqual(ctx.exception.status, 400)
        self.assertNotIn(TOKEN, str(ctx.exception))
        self.assertNotIn(TOKEN, repr(self.bot))
        self.assertEqual(len(self.mock.messages()), 1)

    def test_gives_up_after_max_retries(self):
        self.mock.fail["123456789012345678"] = [429] * 10
        with self.assertRaises(DiscordError):
            self.bot.send_message("123456789012345678", "x")
        self.assertEqual(len(self.mock.messages()), 4)

    def test_bad_channel_id_is_rejected_before_request(self):
        with self.assertRaises(DiscordError):
            self.bot.send_message("not-a-number", "x")
        self.assertEqual(self.mock.requests, [])

    def test_empty_token_rejected(self):
        with self.assertRaises(ValueError):
            DiscordBot("", self.mock.base)

    def test_unreachable_server(self):
        bot = DiscordBot(TOKEN, "http://127.0.0.1:9", sleep=lambda s: None, max_retries=2, timeout=2)
        with self.assertRaises(DiscordError):
            bot.send_message("123456789012345678", "x")

    def test_dry_run_bot_records(self):
        lines = []
        bot = DryRunBot(out=lines.append)
        bot.send_message("1", "hello")
        self.assertEqual(bot.sent, [("1", "hello")])


class MessagesTest(unittest.TestCase):
    def test_diff_message_fits_even_with_huge_input(self):
        for chunk in ("あ" * 1450, "```" * 800, "x" * 10000):
            msg = build_diff_message(status="CHANGED", name="採用情報 - 会社", url="https://e.example/" + "a" * 300,
                                     archive_url="https://web.archive.org/web/1/" + "b" * 300, archive_status="",
                                     chunk=chunk, index=1, total=3)
            self.assertLessEqual(len(msg), DISCORD_LIMIT)
            self.assertTrue(msg.rstrip().endswith("```"))
            self.assertEqual(msg.count("```"), 2)  # 本文中の ``` は無害化される

    def test_error_message_fits(self):
        self.assertLessEqual(len(build_error_message(name="n", url="u", body="エ" * 9000)), DISCORD_LIMIT)

    def test_split_long_text(self):
        text = "\n".join(f"行{i}" + "あ" * 50 for i in range(200))
        chunks = split_long_text(text, 1450)
        self.assertTrue(all(len(c) <= 1450 for c in chunks))
        self.assertEqual("".join(chunks).replace("\n", ""), text.replace("\n", ""))
        self.assertTrue(all(len(c) <= 100 for c in split_long_text("x" * 250, 100)))  # 1行が長すぎる場合も分割

    def test_sns_message(self):
        msg = build_sns_message(company_name="CloverWorks", platform_label="X (旧Twitter)", account_label="メイン",
                                text="動画研修生を募集します\n\n\n\n詳細は…", url="https://x.com/a/status/1",
                                published="2026-10-29T12:45:00Z")
        self.assertIn("**SNS: CloverWorks (X (旧Twitter) / メイン)**", msg)
        self.assertIn("2026-10-29 21:45", msg)  # 日本時間
        self.assertNotIn("\n\n\n", msg)
        long = build_sns_message(company_name="a", platform_label="b", account_label="", text="あ" * 3000, url="u", published=None)
        self.assertLessEqual(len(long), DISCORD_LIMIT)

    def test_format_jst(self):
        self.assertEqual(format_jst("2026-10-29T15:00:00+00:00"), "2026-10-30 00:00")
        self.assertEqual(format_jst("読めない日付"), "読めない日付")
        self.assertEqual(format_jst(None), "")

    def test_feed_error_message(self):
        msg = build_feed_error_message([(f"k{i}", "HTTP 503" * 50) for i in range(30)])
        self.assertLessEqual(len(msg), DISCORD_LIMIT)
        self.assertIn("ほか20件", msg)


if __name__ == "__main__":
    unittest.main()
