#!/usr/bin/env python3
"""会社ファイルから RSSHub のURLを作り、新着を Discord(Bot)に送る。

  python scripts/rsshub_notify.py --list                 # 作られるURLの一覧(送信しない)
  python scripts/rsshub_notify.py --dry-run              # 送信内容を表示(送信も既読の保存もしない)
  python scripts/rsshub_notify.py                        # 本番
  python scripts/rsshub_notify.py --only cloverworks --no-filter --notify-on-first-run --limit 2
                                                         # 配信経路の確認(絞り込みなしの最新2件を送る)

環境変数:
  RSSHUB_BASE_URL     RSSHub のURL(--base でも指定可)
  RSSHUB_ACCESS_KEY   RSSHub のアクセスキー(使っている場合)
  YOUTUBE_API_KEY     YouTube Data API のキー(YouTube の監視に使う)
  DISCORD_BOT_TOKEN   Discord Bot のトークン(--dry-run / --list では不要)

初回(状態ファイルに無いフィード)は、通知せず既読にするだけです(過去分の洪水を防ぐため)。
終了コード: 0=成功 / 1=Discordへの送信に失敗 / 2=設定エラー
"""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from watchlib.config import ROOT, ConfigError, load_config  # noqa: E402
from watchlib.discord import DryRunBot, make_bot  # noqa: E402
from watchlib.rsshub import build_feeds, mask_key  # noqa: E402
from watchlib.runners import run_rsshub  # noqa: E402
from watchlib.state import State  # noqa: E402
from watchlib.validate import ERROR, validate_config  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", type=Path, default=ROOT)
    ap.add_argument("--base", default=os.environ.get("RSSHUB_BASE_URL"))
    ap.add_argument("--state", type=Path, default=None, help="既読の保存先(既定: .rsshub-state/state.json)")
    ap.add_argument("--only", help="この会社(slug か ID)だけ")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--notify-on-first-run", action="store_true", help="初回も通知する(テスト用)")
    ap.add_argument("--no-filter", action="store_true",
                    help="動作確認用: 採用ワードで絞らず、最新の投稿を取る(--notify-on-first-run --limit 2 と併用)")
    ap.add_argument("--limit", type=int, default=5, help="1フィード・1回あたりの最大通知数(既定5)")
    ap.add_argument("--retry-rounds", type=int, default=None, help="取得に失敗したフィードを、全部終わってから再取得する回数(既定: common.yaml の sns.fetch.retry_rounds)")
    ap.add_argument("--retry-wait", type=float, default=None, help="再取得の前に待つ秒数(既定: common.yaml の sns.fetch.retry_wait_seconds)")
    ap.add_argument("--request-interval", type=float, default=None, help="フィードの取得と取得のあいだの秒数(既定: common.yaml の sns.fetch.request_interval_seconds)")
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

    base = args.base or ("https://rsshub.example" if args.list else None)
    if not base:
        print("RSSHub のURLがありません。--base か環境変数 RSSHUB_BASE_URL を指定してください。")
        return 2
    key = os.environ.get("RSSHUB_ACCESS_KEY") or None
    feeds, errors = build_feeds(cfg, base, key, args.only, apply_filter=not args.no_filter)

    if args.list:
        for f in feeds:
            print(f"{f.key}\n    {mask_key(f.request_url)}")
        for label, reason in errors:
            print(f"[組み立て失敗] {label}: {reason}")
        print(f"\nフィード {len(feeds)}件 / 組み立て失敗 {len(errors)}件")
        return 0

    if args.dry_run:
        bot = DryRunBot()
    else:
        token = os.environ.get("DISCORD_BOT_TOKEN")
        if not token:
            print("環境変数 DISCORD_BOT_TOKEN がありません。")
            return 2
        bot = make_bot(cfg, token)

    state = State(args.state or args.root / ".rsshub-state" / "state.json")
    result = run_rsshub(cfg, feeds, state, bot, dry_run=args.dry_run, build_errors=errors,
                        notify_on_first_run=args.notify_on_first_run, limit=args.limit,
                        retry_rounds=args.retry_rounds, retry_wait=args.retry_wait,
                        request_interval=args.request_interval,
                        youtube_api_key=os.environ.get("YOUTUBE_API_KEY") or None)
    print(f"送信 {result.sent}件 / 上限で省略 {result.skipped}件 / 取得エラー {len(result.errors)}件 / 送信失敗 {len(result.send_failures)}件")
    for line in result.send_failures:
        print(f"[送信失敗] {line}")
    return 1 if result.failed else 0


if __name__ == "__main__":
    sys.exit(main())
