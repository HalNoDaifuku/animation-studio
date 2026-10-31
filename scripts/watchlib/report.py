"""urlwatch のテキストレポートを、1ジョブごとの結果に分解する。

レポートの形(urlwatch 2.x):
    サマリー(「01. CHANGED: 名前 ( URL )」の行が並ぶ)
    ----------
    CHANGED: 名前 ( URL )
    ----------
    差分や本文
    ----------   ← 次のジョブ
    ...
    -- 
    urlwatch x.y, Copyright ...
見出しは「前後が区切り線の行」だけを見出しとして扱い、サマリーや本文中の同じ形の行と区別する。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_HEADER = re.compile(r"^(NEW|CHANGED|ERROR|UNCHANGED): (.*?)(?: \( (\S.*?) \))?\s*$")
_SEPARATOR = re.compile(r"^[-=]{10,}\s*$")
_FOOTER = re.compile(r"^-- ?$")


@dataclass
class ReportEntry:
    status: str
    name: str
    url: str
    body: str


def parse_report(text: str, known_names: set[str] | None = None) -> list[ReportEntry]:
    lines = text.splitlines()
    heads: list[tuple[int, re.Match]] = []
    for i in range(1, len(lines) - 1):
        m = _HEADER.match(lines[i])
        if not m or not _SEPARATOR.match(lines[i - 1]) or not _SEPARATOR.match(lines[i + 1]):
            continue
        if known_names is not None and m.group(2) not in known_names:
            continue
        heads.append((i, m))

    entries: list[ReportEntry] = []
    for n, (i, m) in enumerate(heads):
        start = i + 2
        end = heads[n + 1][0] - 1 if n + 1 < len(heads) else len(lines)  # 次の見出しの上の区切り線の手前まで
        body_lines = lines[start:end]
        for j, line in enumerate(body_lines):  # 最後のジョブだけ、フッターを落とす
            if _FOOTER.match(line) and n + 1 == len(heads):
                body_lines = body_lines[:j]
                break
        while body_lines and not body_lines[0].strip():
            body_lines.pop(0)
        while body_lines and (not body_lines[-1].strip() or _SEPARATOR.match(body_lines[-1])):
            body_lines.pop()
        entries.append(ReportEntry(m.group(1), m.group(2), m.group(3) or "", "\n".join(body_lines)))
    return entries
