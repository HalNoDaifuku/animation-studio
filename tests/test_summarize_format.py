import unittest

import helpers  # noqa: F401
from watchlib.summarize import condense_diff, is_format_only, judgement_of


class FormatOnlyTest(unittest.TestCase):
    def test_line_break_only(self):
        self.assertTrue(is_format_only("-P.A.WORKS本文へスキップ\n+P.A.WORKS\n+本文へスキップ"))

    def test_long_line_split_by_newline_condenses_to_nothing(self):
        body = ("--- @\tA\n+++ @\tB\n@@ -1 +1,2 @@\n-P.A.WORKS" + "本文へスキップ。" * 60
                + "\n+P.A.WORKS\n+" + "本文へスキップ。" * 60)
        condensed, _ = condense_diff(body)
        self.assertTrue(is_format_only(condensed))

    def test_real_change_is_not_format_only(self):
        self.assertFalse(is_format_only("-募集中\n+募集終了"))
        self.assertFalse(is_format_only("+原画を募集"))
        self.assertFalse(is_format_only("-原画を募集"))

    def test_judgement_parsed(self):
        self.assertEqual(judgement_of("a\nb\n判定: 変更なし"), "変更なし")


if __name__ == "__main__":
    unittest.main()
