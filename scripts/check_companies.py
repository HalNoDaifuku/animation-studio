#!/usr/bin/env python3
"""会社ファイル(companies/*.yaml)・就活サイトなど(sites/*.yaml)・common.yaml・config/discord.yaml のエラーチェック。

  python scripts/check_companies.py             # エラーがあれば終了コード1
  python scripts/check_companies.py --strict    # 警告があっても終了コード1
  python scripts/check_companies.py --json      # 機械で読める形式で出力
  python scripts/check_companies.py --root DIR  # 別のフォルダを検査(テスト用)
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from watchlib.config import ROOT  # noqa: E402
from watchlib.validate import ERROR, WARNING, validate_config  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", type=Path, default=ROOT)
    ap.add_argument("--strict", action="store_true", help="警告もエラー扱いにする")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    issues = validate_config(args.root)
    errors = [i for i in issues if i.level == ERROR]
    warnings = [i for i in issues if i.level == WARNING]
    if args.json:
        print(json.dumps({"errors": [i.as_dict() for i in errors], "warnings": [i.as_dict() for i in warnings]},
                         ensure_ascii=False, indent=2))
    else:
        for i in errors:
            print(f"[エラー] {i.where}\n    {i.message}")
        for i in warnings:
            print(f"[警告]   {i.where}\n    {i.message}")
        print(f"\nエラー {len(errors)}件 / 警告 {len(warnings)}件")
    if errors or (args.strict and warnings):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
