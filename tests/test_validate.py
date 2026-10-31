import copy
import unittest

import helpers  # noqa: F401
from helpers import load_sample, make_root, write_company
from watchlib.validate import ERROR, WARNING, validate_config

GOOD_ID = "11111111-2222-4333-8444-555555555555"


def issues_for(mutate=None, *, discord=None, extra=None, slug="cloverworks", stem=None):
    company = load_sample(slug)
    if mutate:
        mutate(company)
    root = make_root(companies={stem or slug: company, **(extra or {})}, **(discord or {}))
    return validate_config(root)


def errors(issues):
    return [i for i in issues if i.level == ERROR]


class ValidateTest(unittest.TestCase):
    def assertHasError(self, issues, text):
        found = [i for i in errors(issues) if text in i.message or text in i.where]
        self.assertTrue(found, f"「{text}」を含むエラーが出ませんでした。実際: {[i.message for i in issues]}")

    def test_sample_files_are_clean(self):
        root = make_root(companies={"cloverworks": load_sample("cloverworks"), "bones": load_sample("bones")})
        self.assertEqual(errors(validate_config(root)), [])

    def test_real_repo_files_have_no_errors(self):
        self.assertEqual(errors(validate_config()), [])

    def test_company_file_mistakes(self):
        cases = {
            "id が UUID でない": (lambda c: c.update(id="abc"), "UUID"),
            "slug が大文字": (lambda c: c.update(slug="CloverWorks"), "slug"),
            "slug とファイル名の不一致": (lambda c: c.update(slug="other"), "ファイル名"),
            "display_name が無い": (lambda c: c.pop("display_name"), "display_name"),
            "設立年が文字列": (lambda c: c.update(founded_year="2018年"), "founded_year"),
            "設立年が未来": (lambda c: c.update(founded_year=2999), "founded_year"),
            "website が URL でない": (lambda c: c.update(website="cloverworks.co.jp"), "website"),
            "存在しない職種ID": (lambda c: c["roles"].append("no_such_role"), "no_such_role"),
            "職種の重複": (lambda c: c["roles"].append("in_between"), "重複"),
            "職種の除外語の形が違う": (lambda c: c["roles"].append({"id": "script", "exclude_words": ["a"]}), "add"),
            "schema_version が違う": (lambda c: c.update(schema_version=2), "schema_version"),
            "location が無い": (lambda c: c.pop("location"), "location"),
            "company_id が UUID でない": (lambda c: c["related_companies"][0].update(company_id="x"), "company_id"),
        }
        for label, (mutate, expect) in cases.items():
            with self.subTest(label):
                self.assertHasError(issues_for(mutate), expect)

    def test_sns_mistakes(self):
        cases = {
            "URLをそのまま書いた": (lambda c: c["sns"]["accounts"]["x"][0].update(handle="https://x.com/CloverWorks"), "handle"),
            "空白入りのhandle": (lambda c: c["sns"]["accounts"]["x"][0].update(handle="Clover Works"), "handle"),
            "未定義のプラットフォーム": (lambda c: c["sns"]["accounts"].update(tiktok=[{"handle": "a", "monitor": True}]), "tiktok"),
            "アカウントの重複": (lambda c: c["sns"]["accounts"]["x"].append({"handle": "@cloverworks", "monitor": True}), "重複"),
            "monitor が文字列": (lambda c: c["sns"]["accounts"]["x"][0].update(monitor="yes"), "monitor"),
            "未定義のプリセット": (lambda c: c["sns"]["search"].update(use_presets=["nope"]), "nope"),
            "include が空": (lambda c: c["sns"]["search"].update(default_filter={"include": {"remove": [
                "採用", "募集", "求人", "研修生", "説明会", "新卒", "中途", "通年採用", "スタッフ募集"]}}), "含める語が空"),
            "add/remove 以外のキー": (lambda c: c["sns"]["search"].update(default_filter={"include": {"adds": ["x"]}}), "add"),
            "include と exclude の両方": (lambda c: c["sns"]["search"].update(default_filter={"exclude": {"add": ["採用"]}}), "両方"),
            "クエリが長すぎる": (lambda c: c["sns"]["search"].update(keywords=["あ" * 300, "い" * 300], use_presets=["company_recruit"]), "長すぎ"),
        }
        for label, (mutate, expect) in cases.items():
            with self.subTest(label):
                self.assertHasError(issues_for(mutate), expect)

    def test_monitor_mistakes(self):
        def m(c, **kw):
            c["monitors"][0].update(kw)
        cases = {
            "URLが不正": (lambda c: m(c, url="www.example.com"), "url"),
            "idが不正": (lambda c: m(c, id="Recruit Page"), "id"),
            "会社にない職種": (lambda c: m(c, roles=["storyboard"]), "storyboard"),
            "存在しない snippets": (lambda c: m(c, snippets=["nope"]), "nope"),
            "存在しない skip": (lambda c: m(c, skip_common_excludes=["nope"]), "nope"),
            "urlwatch に name": (lambda c: m(c, urlwatch={"name": "x"}), "自動で組み立てる"),
            "壊れた正規表現": (lambda c: m(c, urlwatch={"filter": [{"re.sub": {"pattern": "(unclosed", "repl": ""}}]}), "正規表現"),
            "re.sub に pattern が無い": (lambda c: m(c, urlwatch={"filter": [{"re.sub": {"repl": ""}}]}), "pattern"),
            "filter がリストでない": (lambda c: m(c, urlwatch={"filter": "html2text"}), "リスト"),
            "max_tries が文字列": (lambda c: m(c, urlwatch={"max_tries": "3"}), "max_tries"),
            "enabled が文字列": (lambda c: m(c, enabled="true"), "enabled"),
            "id の重複": (lambda c: c["monitors"].append(copy.deepcopy(c["monitors"][0])), "重複"),
        }
        for label, (mutate, expect) in cases.items():
            with self.subTest(label):
                self.assertHasError(issues_for(mutate), expect)

    def test_cross_company_duplicates(self):
        clone = load_sample("cloverworks")
        issues = issues_for(extra={"clover2": {**clone, "slug": "clover2"}})
        self.assertHasError(issues, "id が")                       # ID の重複
        self.assertHasError(issues, "ジョブ名")                     # urlwatch ジョブ名の重複
        clone2 = {**clone, "slug": "cloverworks", "id": GOOD_ID}
        self.assertHasError(issues_for(extra={"zz": clone2}), "slug")

    def test_unknown_key_is_warning(self):
        issues = issues_for(lambda c: c.update(foundd_year=2018))
        self.assertEqual(errors(issues), [])
        self.assertTrue(any(i.level == WARNING and "foundd_year" in i.message for i in issues))

    def test_missing_files_and_bad_yaml(self):
        root = make_root(companies={"cloverworks": load_sample("cloverworks")})
        (root / "companies" / "broken.yaml").write_text("id: [未閉じ\n", encoding="utf-8")
        self.assertHasError(validate_config(root), "YAMLとして読めません")
        (root / "companies" / "broken.yaml").write_text("- ただのリスト\n", encoding="utf-8")
        self.assertHasError(validate_config(root), "辞書ではありません")
        (root / "common.yaml").unlink()
        self.assertHasError(validate_config(root), "見つかりません")

    def test_common_yaml_mistakes(self):
        import yaml
        base = yaml.safe_load((helpers.REPO / "common.yaml").read_text(encoding="utf-8"))

        def run(mutate):
            data = copy.deepcopy(base)
            mutate(data)
            root = make_root(companies={"cloverworks": load_sample("cloverworks")})
            (root / "common.yaml").write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
            return validate_config(root)

        self.assertHasError(run(lambda d: d["job_roles"]["storyboard"].update(exclude_words=["コンテ"])), "自分で消して")
        self.assertHasError(run(lambda d: d["job_roles"]["storyboard"].update(keywords=[])), "keywords")
        self.assertHasError(run(lambda d: d["job_roles"]["storyboard"].update(name=d["job_roles"]["script"]["name"])), "重複")
        self.assertHasError(run(lambda d: d["job_roles"].update({"BadId": {"name": "x", "keywords": ["x"], "exclude_words": []}})), "snake_case")
        self.assertHasError(run(lambda d: d["job_roles"]["script"].update(keywords=['ab"c'])), '"')
        self.assertHasError(run(lambda d: d["sns"]["platforms"]["x"].update(user_feed="{base}/x")), "user_feed")
        self.assertHasError(run(lambda d: d["sns"]["platforms"]["x"].update(search_feed="{base}/x")), "search_feed")
        self.assertHasError(run(lambda d: d["sns"]["search_presets"]["role_recruit"]["x"].update(query="({typo})")), "プレースホルダ")
        self.assertHasError(run(lambda d: d["sns"].update(search_max_query_length=20)), "search_max_query_length")
        self.assertHasError(run(lambda d: d["web"]["excludes"]["strip_script_style"].update(filter=[{"re.sub": {"pattern": "(", "repl": ""}}])), "正規表現")
        self.assertHasError(run(lambda d: d["web"]["excludes"]["strip_script_style"].pop("auto")), "auto")
        self.assertHasError(run(lambda d: d["sns"]["default_filter"]["exclude"].append("採用")), "両方")


class DiscordConfigTest(unittest.TestCase):
    def check(self, **discord):
        return validate_config(make_root(companies={"cloverworks": load_sample("cloverworks")}, **discord))

    def test_good_and_bad_ids(self):
        ok = self.check(roles_to_channels={"key_animator": "123456789012345678"}, fallback="123456789012345679")
        self.assertEqual(errors(ok), [])
        bad = self.check(roles_to_channels={"key_animator": "abc", "no_such": "123456789012345678"})
        self.assertTrue(any("チャンネルID" in i.message for i in errors(bad)))
        self.assertTrue(any("job_roles" in i.message for i in errors(bad)))

    def test_int_ids_warn_and_duplicates_warn(self):
        w = self.check(roles_to_channels={"key_animator": 123456789012345678, "in_between": 123456789012345678})
        msgs = " ".join(i.message for i in w if i.level == WARNING)
        self.assertIn("囲んで", msgs)
        self.assertIn("1回しか", msgs)

    def test_unmapped_roles_warn(self):
        w = self.check(roles_to_channels={"key_animator": "123456789012345678"})
        self.assertTrue(any("未割り当て" in i.message and "fallback も未設定" in i.message for i in w))
        w2 = self.check(roles_to_channels={}, fallback="123456789012345678")
        self.assertTrue(any("fallback に送られます" in i.message for i in w2))

    def test_missing_discord_file_is_only_a_warning(self):
        root = make_root(companies={"cloverworks": load_sample("cloverworks")})
        (root / "config" / "discord.yaml").unlink()
        issues = validate_config(root)
        self.assertEqual(errors(issues), [])
        self.assertTrue(any("config/discord.yaml" in i.where for i in issues))


if __name__ == "__main__":
    unittest.main()
