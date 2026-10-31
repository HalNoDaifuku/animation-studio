#!/usr/bin/env python3
"""会社ファイルの monitors から urls.yaml を作り、urlwatch を実行して、結果を Discord(Bot)に送る。

  python scripts/urlwatch_notify.py --generate-only      # urls.yaml を作るだけ(urlwatch も送信もしない)
  python scripts/urlwatch_notify.py --dry-run            # urlwatch は(キャッシュのコピーで)実行し、送信内容を表示。キャッシュは進めない
  python scripts/urlwatch_notify.py --skip-new           # 初回(全部NEW)の通知を出さずにキャッシュだけ作る
  python scripts/urlwatch_notify.py --report FILE        # urlwatch を実行せず、保存済みのレポートを送る

既存の urls.yaml / キャッシュとは別のファイル(build/urls.yaml / .urlwatch/cache-companies.db)を使うので、
いまの監視を壊しません。環境変数 DISCORD_BOT_TOKEN が必要です(--dry-run / --generate-only では不要)。
終了コード: 0=成功 / 1=urlwatch の実行失敗・Discordへの送信失敗 / 2=設定エラー
"""
import argparse
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from watchlib.config import ROOT, ConfigError, load_config  # noqa: E402
from watchlib.discord import DiscordBot, DryRunBot  # noqa: E402
from watchlib.messages import truncate  # noqa: E402
from watchlib.runners import RunResult, run_urlwatch_command, run_urlwatch_report, send_to_errors_channel  # noqa: E402
from watchlib.urlwatch_jobs import build_jobs, write_urls_yaml  # noqa: E402
from watchlib.validate import ERROR, validate_config  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", type=Path, default=ROOT)
    ap.add_argument("--only", help="この会社(slug か ID)だけ")
    ap.add_argument("--urls-out", type=Path, default=None, help="生成する urls.yaml(既定: build/urls.yaml)")
    ap.add_argument("--cache", type=Path, default=None, help="urlwatch のキャッシュ(既定: .urlwatch/cache-companies.db)")
    ap.add_argument("--urlwatch-cmd", default="urlwatch")
    ap.add_argument("--report", type=Path, help="実行せず、このレポートファイルを送る")
    ap.add_argument("--generate-only", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--skip-new", action="store_true")
    ap.add_argument("--wayback", action="store_true", help="変更を Wayback Machine に保存してURLを付ける")
    args = ap.parse_args(argv)

    problems = [i for i in validate_config(args.root) if i.level == ERROR]
    if problems:
        print("設定にエラーがあるため中止します。python scripts/check_companies.py で確認してください。")
        for i in problems[:10]:
            print(f"  {i.where}: {i.message}")
        return 2
    try:
        cfg = load_config(args.root)
    except ConfigError as e:
        print(e)
        return 2

    jobs, infos = build_jobs(cfg, args.only)
    if not jobs:
        print("監視するURLがありません。")
        return 2
    urls_path = args.urls_out or args.root / "build" / "urls.yaml"
    write_urls_yaml(jobs, urls_path)
    print(f"{urls_path} に {len(jobs)}件のジョブを書きました。")
    if args.generate_only:
        return 0

    if args.dry_run:
        bot = DryRunBot()
    else:
        token = os.environ.get("DISCORD_BOT_TOKEN")
        if not token:
            print("環境変数 DISCORD_BOT_TOKEN がありません。")
            return 2
        bot = DiscordBot(token, cfg.discord_channels["api_base"])

    result = RunResult()
    if args.report:
        report_text = args.report.read_text(encoding="utf-8", errors="replace")
    else:
        cache = args.cache or args.root / ".urlwatch" / "cache-companies.db"
        tmp_dir = None
        if args.dry_run:  # 試し実行でキャッシュを進めない(本番で変更を見逃さないため)。コピーで実行する
            tmp_dir = tempfile.TemporaryDirectory()
            copy = Path(tmp_dir.name) / "cache.db"
            if cache.exists():
                shutil.copy(cache, copy)
            cache = copy
        try:
            code, report_text, err = run_urlwatch_command(urls_path, cache, args.urlwatch_cmd)
        except FileNotFoundError:
            print(f"urlwatch が見つかりません({args.urlwatch_cmd})。pip install urlwatch で入れてください。")
            return 1
        finally:
            if tmp_dir:
                tmp_dir.cleanup()
        if code != 0:
            msg = f"**urlwatch の実行に失敗しました(終了コード {code})**\n```\n{truncate(err.strip() or report_text.strip(), 1500)}\n```"
            print(msg)
            send_to_errors_channel(cfg, bot, msg, result, dry_run=args.dry_run)
            return 1

    sent = run_urlwatch_report(cfg, report_text, infos, bot, dry_run=args.dry_run,
                               skip_new=args.skip_new, use_wayback=args.wayback)
    sent.sent += result.sent
    print(f"送信 {sent.sent}件 / 省略(NEW) {sent.skipped}件 / 送信失敗 {len(sent.send_failures)}件")
    for line in sent.send_failures + result.send_failures:
        print(f"[送信失敗] {line}")
    return 1 if (sent.failed or result.failed) else 0


if __name__ == "__main__":
    sys.exit(main())
