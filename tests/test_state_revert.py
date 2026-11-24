import unittest

import helpers  # noqa: F401
from watchlib.state import State

H = 3600


class ObserveContentTest(unittest.TestCase):
    def test_flip_flop_is_revert_until_state_is_stable(self):
        st = State(None)
        self.assertEqual(st.observe_content("k", "A", 4, now=0, stable_hours=12), "new")
        self.assertEqual(st.observe_content("k", "A", 4, now=1 * H, stable_hours=12), "same")
        self.assertEqual(st.observe_content("k", "B", 4, now=2 * H, stable_hours=12), "change")
        self.assertEqual(st.observe_content("k", "A", 4, now=4 * H, stable_hours=12), "revert")        # B は2時間だけ
        self.assertEqual(st.observe_content("k", "B", 4, now=6 * H, stable_hours=12), "revert")
        self.assertEqual(st.observe_content("k", "A", 4, now=6 * H + 13 * H, stable_hours=12), "stable-revert")  # B が13時間続いた

    def test_keeps_only_the_last_n_states(self):
        st = State(None)
        for i, h in enumerate("ABCDE"):
            st.observe_content("k", h, 4, now=i)
        self.assertEqual([x[0] for x in st.contents["k"]], list("BCDE"))
        self.assertEqual(st.observe_content("k", "A", 4, now=10, stable_hours=12), "change")   # 忘れた内容は、新しい扱い

    def test_commit_false_does_not_change_state(self):
        st = State(None)
        st.observe_content("k", "A", 4, now=0)
        st.observe_content("k", "B", 4, commit=False, now=1)
        self.assertEqual([x[0] for x in st.contents["k"]], ["A"])

    def test_save_load_and_old_format(self):
        import json, tempfile
        from pathlib import Path
        path = Path(tempfile.mkdtemp()) / "s.json"
        st = State(path)
        st.observe_content("k", "A", 4, now=5)
        st.save()
        self.assertEqual(State(path).contents, {"k": [["A", 5.0]]})
        path.write_text(json.dumps({"contents": {"k": ["A", "B"]}}), encoding="utf-8")   # 前の形式(ハッシュだけ)
        self.assertEqual([x[0] for x in State(path).contents["k"]], ["A", "B"])


if __name__ == "__main__":
    unittest.main()
