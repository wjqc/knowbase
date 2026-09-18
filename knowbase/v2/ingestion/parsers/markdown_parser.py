"""Markdown 解析器（P1-B）。

特性：
- 保留 # / ## 标题为结构化 meta（heading 列表）
- 正文中段间用 \n\n；列表项前缀统一为 "- " 方便下游 chunking
- 不做 frontmatter 解析（V1 store.parse 负责；V2 走 Source 层配置）

注：与 V1 store.parse 行为兼容，正文 plain text。
"""
from __future__ import annotations

import re
from pathlib import Path

from .base import DocumentParser, ParsedDocument


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$", re.MULTILINE)
_LIST_RE = re.compile(r"^(\s*)[-*+]\s+", re.MULTILINE)
_FENCE_RE = re.compile(r"^```.*$", re.MULTILINE)


class MarkdownParser(DocumentParser):
    name = "markdown"

    def supports(self, path: Path) -> bool:
        return path.suffix.lower() in (".md", ".markdown", ".mdx")

    def parse(self, path: Path) -> ParsedDocument:
        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            from ...observability.errors import ParseError
            raise ParseError(parser=self.name, reason=str(e), path=str(path)) from e

        text = raw.replace("\r\n", "\n").replace("\r", "\n")
        headings = [m.group(2).strip() for m in _HEADING_RE.finditer(text)]
        title = headings[0] if headings else path.stem

        return ParsedDocument(
            text=text,
            meta={"title": title, "headings": headings, "format": "markdown"},
        )
