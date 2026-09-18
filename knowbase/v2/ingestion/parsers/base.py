"""解析器抽象与注册表（P1-B）。

设计：
- DocumentParser.supports(path) -> bool：按扩展名 / MIME 判定
- DocumentParser.parse(path) -> ParsedDocument：bytes/text + meta
- ParsedDocument.text 是归一化后的纯文本（去除 \r，统一 \n）
- meta 字段保留结构化信息（title / headings / page_count / author）
- 解析失败抛 ParseError（含 parser 名 + 原因 + path），由 service 决定重试 / tombstone
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


@dataclass
class ParsedDocument:
    text: str
    meta: dict = field(default_factory=dict)


class DocumentParser(ABC):
    name: str = "abstract"

    @abstractmethod
    def supports(self, path: Path) -> bool: ...

    @abstractmethod
    def parse(self, path: Path) -> ParsedDocument: ...


class ParserRegistry:
    def __init__(self):
        self._parsers: list[DocumentParser] = []

    def register(self, parser: DocumentParser) -> None:
        # 按 name 去重（同一类型不应重复注册；不同实例也视为同类型）
        for p in self._parsers:
            if p.name == parser.name:
                return
        self._parsers.append(parser)

    def find(self, path: Path) -> DocumentParser | None:
        for p in self._parsers:
            try:
                if p.supports(path):
                    return p
            except Exception:
                continue
        return None

    def parsers(self) -> Iterable[DocumentParser]:
        return tuple(self._parsers)


# 单例
_default = ParserRegistry()


def registry() -> ParserRegistry:
    return _default


def register_builtin() -> None:
    """注册 10 个内置解析器；幂等。

    注册顺序敏感：
    - LogParser 必须在 TxtParser 之前（两者都声明 .log；前者更具体）
    - CodeParser / HtmlParser 必须在 TxtParser 之前（更具体的扩展名）
    - OcrParser 始终注册（即使 tesseract 缺失）；parse 时检查
    """
    from .markdown_parser import MarkdownParser
    from .html_parser import HtmlParser
    from .code_parser import CodeParser
    from .log_parser import LogParser
    from .txt_parser import TxtParser
    from .pdf_parser import PdfParser
    from .docx_parser import DocxParser
    from .xlsx_parser import XlsxParser
    from .pptx_parser import PptxParser
    from .ocr_parser import OcrParser

    reg = registry()
    for p in (MarkdownParser(), HtmlParser(), CodeParser(), LogParser(),
              TxtParser(), PdfParser(), DocxParser(),
              XlsxParser(), PptxParser(), OcrParser()):
        reg.register(p)
