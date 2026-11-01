#!/usr/bin/env python3
"""展開したファイルが、渡したZIPと一致しているかを照合する(docs/FILES.sha256 と比べる)。

  python scripts/verify_files.py

  [違い]  内容が違う(上書きされていない、または自分で編集した)
  [不足]  ZIPにあるのに、リポジトリに無い
  [余分]  ZIPに無いファイルがある(古いファイル・削除し忘れの可能性)
改行コード(CRLF/LF)の違いは無視します。自分で編集したファイル(config/discord.yaml のチャンネルID など)は「違い」に出ます。
終了コード: 0=すべて一致 / 1=違い・不足・余分がある
"""
import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "docs" / "FILES.sha256"
IGNORE_DIRS = {".git", "build", ".rsshub-state", ".urlwatch", ".urlwatch-last-good", "__pycache__", ".venv", "venv", "node_modules"}
IGNORE_FILES = {".DS_Store", "dot_DS_Store", "Icon\r"}


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def main() -> int:
    if not MANIFEST.exists():
        print(f"{MANIFEST} がありません。")
        return 1
    expected: dict[str, str] = {}
    for line in MANIFEST.read_text(encoding="utf-8").splitlines():
        if line.strip():
            h, _, name = line.partition("  ")
            expected[name.strip()] = h
    problems = 0
    for name, h in sorted(expected.items()):
        path = ROOT / name
        if not path.exists():
            print(f"[不足] {name}")
            problems += 1
        elif digest(path) != h:
            print(f"[違い] {name}")
            problems += 1
    for path in sorted(ROOT.rglob("*")):
        rel = path.relative_to(ROOT)
        if path.is_dir() or IGNORE_DIRS & set(rel.parts) or path.name in IGNORE_FILES or path.suffix == ".pyc":
            continue
        name = rel.as_posix()
        if name not in expected and name != "docs/FILES.sha256":
            print(f"[余分] {name}")
            problems += 1
    print(f"\n{len(expected)}ファイルを照合: " + ("すべて一致しました。" if not problems else f"{problems}件の違いがあります。"))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
