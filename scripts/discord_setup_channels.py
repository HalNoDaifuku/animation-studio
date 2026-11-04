#!/usr/bin/env python3
"""Discord サーバーに、職種ごとのチャンネルを作る(Bot で)。

  DISCORD_BOT_TOKEN=... DISCORD_GUILD_ID=... python scripts/discord_setup_channels.py           # 計画を表示だけ
  DISCORD_BOT_TOKEN=... DISCORD_GUILD_ID=... python scripts/discord_setup_channels.py --apply   # 実際に作る

作るのは、会社ファイルで使われている職種のチャンネル + 未分類(fallback) + 監視エラー(errors)。
同じ名前のチャンネルが既にあれば再利用します。最後に config/discord.yaml へ貼る YAML を表示します。
Bot には「チャンネルの管理」権限が必要です。サーバーIDは、開発者モードで サーバー名を右クリック →「サーバーIDをコピー」。
"""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from watchlib.channels import apply_plan, mapping_yaml, plan_channels, used_role_ids  # noqa: E402
from watchlib.config import ROOT, ConfigError, load_config  # noqa: E402
from watchlib.discord import make_bot, DiscordError  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", type=Path, default=ROOT)
    ap.add_argument("--guild", default=os.environ.get("DISCORD_GUILD_ID"))
    ap.add_argument("--category", default="アニメ会社 採用監視")
    ap.add_argument("--all-roles", action="store_true", help="使われていない職種も含め、全職種のチャンネルを作る")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args(argv)

    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token or not args.guild:
        print("環境変数 DISCORD_BOT_TOKEN と DISCORD_GUILD_ID(または --guild)が必要です。")
        return 2
    try:
        cfg = load_config(args.root)
    except ConfigError as e:
        print(e)
        return 2
    role_ids = list(cfg.job_roles) if args.all_roles else used_role_ids(cfg)
    bot = make_bot(cfg, token)
    try:
        existing = bot.list_guild_channels(args.guild)
        plan = plan_channels(cfg, existing, args.category, role_ids)
        for p in plan:
            state = f"既存 {p.existing_id}" if p.existing_id else ("作成" if args.apply else "作成予定")
            print(f"[{state}] #{p.name}  ({p.category} / {p.topic})")
        ids: dict = {p.key: p.existing_id for p in plan}
        if args.apply:
            ids = apply_plan(bot, args.guild, plan, existing)
        else:
            print("\n※ --apply を付けるまで、何も作りません。")
    except DiscordError as e:
        print(f"Discord のエラー: {e}")
        return 1
    print("\n# config/discord.yaml の channels に貼る内容\n" + mapping_yaml(cfg, plan, ids))
    return 0


if __name__ == "__main__":
    sys.exit(main())
