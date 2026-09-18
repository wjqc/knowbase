"""PDF 解析器（P1-B）。

基于 pypdf：
- 每页文本用 \n\n 分隔，便于下游按段 chunk
- meta 记录 page_count
- 加密 PDF 抛 ParseError（Phase 5 再加口令）
"""
from __future__ import annotations

from pathlib import Path

from .base import DocumentParser, ParsedDocument


class PdfParser(DocumentParser):
    name = "pdf"

    def supports(self, path: Path) -> bool:
        return path.suffix.lower() == ".pdf"

    def parse(self, path: Path) -> ParsedDocument:
        from .errors import ParseError
        try:
            from pypdf import PdfReader
        except ImportError as e:
            raise ParseError(parser=self.name,
                             reason="pypdf not installed; uv add pypdf",
                             path=str(path)) from e
        try:
            reader = PdfReader(str(path))
        except Exception as e:
            raise ParseError(parser=self.name, reason=f"cannot open: {e}",
                             path=str(path)) from e

        if getattr(reader, "is_encrypted", False):
            raise ParseError(parser=self.name, reason="encrypted; password not supported in P1",
                             path=str(path))

        pages: list[str] = []
        for i, page in enumerate(reader.pages):
            try:
                txt = page.extract_text() or ""
            except Exception as e:  # 单页失败不阻断整文件
                txt = f"[page {i + 1} extract failed: {e}]"
            pages.append(txt.strip())

        text = "\n\n".join(p for p in pages if p)
        return ParsedDocument(
            text=text,
            meta={
                "format": "pdf",
                "page_count": len(reader.pages),
                "title": path.stem,
            },
        )
