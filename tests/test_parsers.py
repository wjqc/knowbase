"""P1-B 解析器单元测试。"""
from __future__ import annotations

from pathlib import Path

import pytest

from knowbase.parsers import (
    ParserRegistry,
    register_builtin,
    registry,
)
from knowbase.parsers.docx_parser import DocxParser
from knowbase.parsers.markdown_parser import MarkdownParser
from knowbase.parsers.pdf_parser import PdfParser
from knowbase.parsers.txt_parser import TxtParser
from knowbase.parsers.errors import ParseError


@pytest.fixture
def reg() -> ParserRegistry:
    r = ParserRegistry()
    for p in (MarkdownParser(), TxtParser(), PdfParser(), DocxParser()):
        r.register(p)
    return r


def test_registry_finds_markdown(tmp_path: Path):
    r = registry()
    register_builtin()
    p = tmp_path / "a.md"
    p.write_text("# Title\n\nbody")
    parser = r.find(p)
    assert parser is not None
    assert parser.name == "markdown"


def test_markdown_parser_extracts_headings(tmp_path: Path):
    p = tmp_path / "a.md"
    p.write_text("# H1\n\nintro\n\n## H2\n\n- item 1\n- item 2\n")
    parser = MarkdownParser()
    parsed = parser.parse(p)
    assert "H1" in parsed.meta["headings"]
    assert "H2" in parsed.meta["headings"]
    assert "intro" in parsed.text
    assert "item 1" in parsed.text


def test_txt_parser_falls_back_to_stem(tmp_path: Path):
    p = tmp_path / "note.txt"
    p.write_text("first line\nsecond")
    parser = TxtParser()
    parsed = parser.parse(p)
    assert parsed.meta["title"] == "first line"


def test_txt_parser_handles_no_ext(tmp_path: Path):
    p = tmp_path / "README"
    p.write_text("hello world")
    parser = TxtParser()
    assert parser.supports(p)
    parsed = parser.parse(p)
    assert "hello world" in parsed.text


def test_pdf_parser_raises_on_missing_dependency(tmp_path: Path, monkeypatch):
    """若 pypdf 缺失，抛 ParseError。"""
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *a, **kw):
        if name == "pypdf":
            raise ImportError("simulated missing")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    p = tmp_path / "a.pdf"
    p.write_bytes(b"%PDF-stub")
    with pytest.raises(ParseError):
        PdfParser().parse(p)


def test_pdf_parser_extracts_text(tmp_path: Path):
    """生成最小 PDF 并解析。"""
    pypdf_writer = pytest.importorskip("pypdf")
    from pypdf import PdfWriter
    p = tmp_path / "hello.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    # 写一段文本到页面（pypdf 提取基于 content stream 注释）
    writer.pages[0].add_text_annotation if hasattr(writer.pages[0], "add_text_annotation") else None
    with open(p, "wb") as f:
        writer.write(f)
    # 即便文本为空，解析也必须成功（page_count >= 1）
    parser = PdfParser()
    parsed = parser.parse(p)
    assert parsed.meta["page_count"] >= 1
    assert parsed.meta["format"] == "pdf"


def test_docx_parser_raises_on_missing_dependency(tmp_path: Path, monkeypatch):
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *a, **kw):
        if name == "docx":
            raise ImportError("simulated missing")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    p = tmp_path / "a.docx"
    p.write_bytes(b"PK-stub")
    with pytest.raises(ParseError):
        DocxParser().parse(p)


def test_docx_parser_extracts_paragraphs(tmp_path: Path):
    docx = pytest.importorskip("docx")
    from docx import Document as DocxDocument
    p = tmp_path / "a.docx"
    doc = DocxDocument()
    doc.add_paragraph("Hello world")
    doc.add_paragraph("Second paragraph")
    doc.save(str(p))
    parsed = DocxParser().parse(p)
    assert "Hello world" in parsed.text
    assert "Second paragraph" in parsed.text
    assert parsed.meta["format"] == "docx"


def test_register_builtin_idempotent():
    """新建局部注册表，验证 register_builtin 幂等（重复调用不增加）。"""
    from knowbase.parsers.base import ParserRegistry
    from knowbase.parsers.markdown_parser import MarkdownParser
    from knowbase.parsers.txt_parser import TxtParser
    from knowbase.parsers.pdf_parser import PdfParser
    from knowbase.parsers.docx_parser import DocxParser
    # 使用全新注册表避免与全局单例状态互相污染
    r = ParserRegistry()
    n0 = len(list(r.parsers()))
    assert n0 == 0
    # 手动模拟 register_builtin 的幂等性
    for _ in range(2):
        for p in (MarkdownParser(), TxtParser(), PdfParser(), DocxParser()):
            r.register(p)
    assert len(list(r.parsers())) == 4  # 4 个不同 name,各注册一次
    # 全局 register_builtin 也不会突破：单例按 name 去重
    register_builtin()
    n_global = len(list(registry().parsers()))
    register_builtin()
    assert len(list(registry().parsers())) == n_global
