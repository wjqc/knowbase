"""TXT 解析器（P1-B）。

特性：
- 兜底解析器（.txt / 无扩展名 / 未知扩展名）
- 归一化行尾
- 尝试按首个非空行作为 title
"""
from __future__ import annotations

from pathlib import Path

from .base import DocumentParser, ParsedDocument


class TxtParser(DocumentParser):
    name = "txt"

    def supports(self, path: Path) -> bool:
        return path.suffix.lower() in (".txt", ".log", "") or path.suffix.lower() is None

    def parse(self, path: Path) -> ParsedDocument:
        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            from .errors import ParseError
            raise ParseError(parser=self.name, reason=str(e), path=str(path)) from e

        text = raw.replace("\r\n", "\n").replace("\r", "\n").strip("\n")
        title = ""
        for line in text.splitlines():
            if line.strip():
                title = line.strip()[:80]
                break
        if not title:
            title = path.stem or path.name

        return ParsedDocument(text=text, meta={"title": title, "format": "plain"})
