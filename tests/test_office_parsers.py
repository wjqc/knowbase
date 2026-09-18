"""P5-A1 XLSX / PPTX 解析器单元测试。"""
from __future__ import annotations

import pytest

from knowbase.parsers import ParserRegistry, register_builtin, registry
from knowbase.parsers.docx_parser import DocxParser
from knowbase.parsers.markdown_parser import MarkdownParser
from knowbase.parsers.pdf_parser import PdfParser
from knowbase.parsers.pptx_parser import PptxParser
from knowbase.parsers.txt_parser import TxtParser
from knowbase.parsers.xlsx_parser import XlsxParser
from knowbase.parsers.errors import ParseError


# -----------------------
# XlsxParser
# -----------------------

class TestXlsxParser:
    def test_extracts_sheet_and_rows(self, tmp_path):
        openpyxl = pytest.importorskip("openpyxl")
        from openpyxl import Workbook
        p = tmp_path / "data.xlsx"
        wb = Workbook()
        ws = wb.active
        ws.title = "Sales"
        ws.append(["name", "amount"])
        ws.append(["alice", "100"])
        ws.append(["bob", "200"])
        wb.save(str(p))

        parsed = XlsxParser().parse(p)
        assert parsed.meta["format"] == "xlsx"
        assert parsed.meta["title"] == "Sales"
        assert parsed.meta["sheet_count"] == 1
        assert parsed.meta["row_count"] == 3
        assert "name | amount" in parsed.text
        assert "alice | 100" in parsed.text
        assert "bob | 200" in parsed.text

    def test_handles_multiple_sheets(self, tmp_path):
        openpyxl = pytest.importorskip("openpyxl")
        from openpyxl import Workbook
        p = tmp_path / "multi.xlsx"
        wb = Workbook()
        ws1 = wb.active
        ws1.title = "First"
        ws1.append(["h1"])
        ws1.append(["v1"])
        ws2 = wb.create_sheet("Second")
        ws2.append(["h2"])
        ws2.append(["v2"])
        wb.save(str(p))

        parsed = XlsxParser().parse(p)
        assert parsed.meta["sheet_count"] == 2
        assert parsed.meta["sheet_names"] == ["First", "Second"]
        assert parsed.meta["row_count"] == 4
        assert "# Sheet: First" in parsed.text
        assert "# Sheet: Second" in parsed.text

    def test_skips_empty_sheets(self, tmp_path):
        openpyxl = pytest.importorskip("openpyxl")
        from openpyxl import Workbook
        p = tmp_path / "with_empty.xlsx"
        wb = Workbook()
        ws1 = wb.active
        ws1.title = "HasData"
        ws1.append(["a", "b"])
        ws1.append(["1", "2"])
        ws2 = wb.create_sheet("Empty")
        # 不加任何行
        wb.save(str(p))

        parsed = XlsxParser().parse(p)
        assert parsed.meta["sheet_count"] == 2  # sheet 存在但内容空
        assert "# Sheet: Empty" not in parsed.text
        assert "# Sheet: HasData" in parsed.text
        assert parsed.meta["row_count"] == 2  # 仅 HasData 的 2 行

    def test_handles_none_cells(self, tmp_path):
        """None / NoneType cell 应转为空串而不是 'None' 字符串。"""
        openpyxl = pytest.importorskip("openpyxl")
        from openpyxl import Workbook
        p = tmp_path / "sparse.xlsx"
        wb = Workbook()
        ws = wb.active
        ws.title = "Sparse"
        ws.append(["a", None, "c"])
        ws.append([None, "x", None])
        wb.save(str(p))

        parsed = XlsxParser().parse(p)
        assert "None" not in parsed.text
        assert "a |  | c" in parsed.text
        # 行尾 trailing 空格在 .text 整体 strip 后会被裁掉,断言不带尾空格
        assert " | x |" in parsed.text

    def test_supports_xlsx_and_xlsm(self, tmp_path):
        parser = XlsxParser()
        assert parser.supports(tmp_path / "a.xlsx")
        assert parser.supports(tmp_path / "a.xlsm")
        assert parser.supports(tmp_path / "A.XLSX")  # 大小写不敏感
        assert not parser.supports(tmp_path / "a.csv")
        assert not parser.supports(tmp_path / "a.txt")

    def test_raises_on_missing_dependency(self, tmp_path, monkeypatch):
        import builtins
        real_import = builtins.__import__

        def fake_import(name, *a, **kw):
            if name == "openpyxl":
                raise ImportError("simulated missing")
            return real_import(name, *a, **kw)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        p = tmp_path / "a.xlsx"
        p.write_bytes(b"PK-stub")
        with pytest.raises(ParseError, match="openpyxl not installed"):
            XlsxParser().parse(p)

    def test_raises_on_corrupt_file(self, tmp_path):
        """非 zip / 损坏 xlsx 抛 ParseError 而非挂掉。"""
        p = tmp_path / "broken.xlsx"
        p.write_bytes(b"NOT-A-ZIP-FILE")
        with pytest.raises(ParseError, match="cannot open"):
            XlsxParser().parse(p)


# -----------------------
# PptxParser
# -----------------------

class TestPptxParser:
    def test_extracts_slide_text(self, tmp_path):
        pptx = pytest.importorskip("pptx")
        from pptx import Presentation
        from pptx.util import Inches
        p = tmp_path / "deck.pptx"
        prs = Presentation()
        prs.slide_width = Inches(10)
        prs.slide_height = Inches(7.5)
        blank_layout = prs.slide_layouts[6]  # Blank 无 title placeholder
        slide1 = prs.slides.add_slide(blank_layout)
        # Blank layout 没有 title placeholder,直接 add textbox
        tx = slide1.shapes.add_textbox(Inches(1), Inches(1), Inches(8), Inches(5))
        tf = tx.text_frame
        tf.text = "Body line 1"
        p2 = tf.add_paragraph()
        p2.text = "Body line 2"
        prs.save(str(p))

        parsed = PptxParser().parse(p)
        assert parsed.meta["format"] == "pptx"
        assert parsed.meta["slide_count"] == 1
        assert "# Slide 1" in parsed.text
        assert "Body line 1" in parsed.text
        assert "Body line 2" in parsed.text

    def test_handles_multiple_slides(self, tmp_path):
        pptx = pytest.importorskip("pptx")
        from pptx import Presentation
        from pptx.util import Inches
        p = tmp_path / "multi.pptx"
        prs = Presentation()
        blank = prs.slide_layouts[6]
        s1 = prs.slides.add_slide(blank)
        s1.shapes.add_textbox(Inches(1), Inches(1), Inches(8), Inches(5)).text_frame.text = "Slide one content"
        s2 = prs.slides.add_slide(blank)
        s2.shapes.add_textbox(Inches(1), Inches(1), Inches(8), Inches(5)).text_frame.text = "Slide two content"
        prs.save(str(p))

        parsed = PptxParser().parse(p)
        assert parsed.meta["slide_count"] == 2
        assert "# Slide 1" in parsed.text
        assert "# Slide 2" in parsed.text
        assert "Slide one content" in parsed.text
        assert "Slide two content" in parsed.text

    def test_includes_notes(self, tmp_path):
        pptx = pytest.importorskip("pptx")
        from pptx import Presentation
        from pptx.util import Inches
        p = tmp_path / "with_notes.pptx"
        prs = Presentation()
        blank = prs.slide_layouts[6]
        slide = prs.slides.add_slide(blank)
        slide.shapes.add_textbox(Inches(1), Inches(1), Inches(8), Inches(5)).text_frame.text = "Visible text"
        notes_slide = slide.notes_slide
        notes_slide.notes_text_frame.text = "Speaker notes here"
        prs.save(str(p))

        parsed = PptxParser().parse(p)
        assert "Visible text" in parsed.text
        assert "[Notes] Speaker notes here" in parsed.text

    def test_skips_empty_slides(self, tmp_path):
        pptx = pytest.importorskip("pptx")
        from pptx import Presentation
        from pptx.util import Inches
        p = tmp_path / "mixed.pptx"
        prs = Presentation()
        blank = prs.slide_layouts[6]
        # 空白 slide
        prs.slides.add_slide(blank)
        # 有内容的 slide
        s2 = prs.slides.add_slide(blank)
        s2.shapes.add_textbox(Inches(1), Inches(1), Inches(8), Inches(5)).text_frame.text = "Real content"
        prs.save(str(p))

        parsed = PptxParser().parse(p)
        assert parsed.meta["slide_count"] == 2
        # 空 slide 编号不应出现(因为没有 parts + notes)
        assert "# Slide 1" not in parsed.text
        assert "# Slide 2" in parsed.text
        assert "Real content" in parsed.text

    def test_meta_includes_author(self, tmp_path):
        pptx = pytest.importorskip("pptx")
        from pptx import Presentation
        from pptx.util import Inches
        p = tmp_path / "auth.pptx"
        prs = Presentation()
        blank = prs.slide_layouts[6]
        slide = prs.slides.add_slide(blank)
        slide.shapes.add_textbox(Inches(1), Inches(1), Inches(8), Inches(5)).text_frame.text = "x"
        prs.core_properties.author = "Alice"
        prs.core_properties.title = "Demo Deck"
        prs.save(str(p))

        parsed = PptxParser().parse(p)
        assert parsed.meta["author"] == "Alice"
        assert parsed.meta["title"] == "Demo Deck"

    def test_falls_back_to_stem_for_title(self, tmp_path):
        pptx = pytest.importorskip("pptx")
        from pptx import Presentation
        from pptx.util import Inches
        p = tmp_path / "no_title.pptx"
        prs = Presentation()
        blank = prs.slide_layouts[6]
        slide = prs.slides.add_slide(blank)
        slide.shapes.add_textbox(Inches(1), Inches(1), Inches(8), Inches(5)).text_frame.text = "x"
        # 不设置 core_properties.title
        prs.save(str(p))

        parsed = PptxParser().parse(p)
        assert parsed.meta["title"] == "no_title"

    def test_supports_pptx_only(self, tmp_path):
        parser = PptxParser()
        assert parser.supports(tmp_path / "a.pptx")
        assert parser.supports(tmp_path / "A.PPTX")
        assert not parser.supports(tmp_path / "a.ppt")  # 老格式不支持
        assert not parser.supports(tmp_path / "a.docx")

    def test_raises_on_missing_dependency(self, tmp_path, monkeypatch):
        import builtins
        real_import = builtins.__import__

        def fake_import(name, *a, **kw):
            if name == "pptx":
                raise ImportError("simulated missing")
            return real_import(name, *a, **kw)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        p = tmp_path / "a.pptx"
        p.write_bytes(b"PK-stub")
        with pytest.raises(ParseError, match="python-pptx not installed"):
            PptxParser().parse(p)

    def test_raises_on_corrupt_file(self, tmp_path):
        """非 zip / 损坏 pptx 抛 ParseError。"""
        p = tmp_path / "broken.pptx"
        p.write_bytes(b"NOT-A-ZIP-FILE")
        with pytest.raises(ParseError, match="cannot open"):
            PptxParser().parse(p)


# -----------------------
# 注册表集成
# -----------------------

class TestRegisterBuiltinExtended:
    def test_register_builtin_includes_xlsx_and_pptx(self):
        """register_builtin 应注册 6 个解析器；幂等。

        说明：全局 registry() 是模块级单例，跨测试可能被其他测试
        污染。改用 monkeypatch 替换 base._default 为一个干净实例来断言
        完整的 6 解析器集合；幂等性则基于同一个全局 singleton 验证。
        """
        from knowbase.parsers import base as _base

        # 隔离：替换 base._default 为一个干净实例
        fresh = ParserRegistry()
        monkey = pytest.MonkeyPatch()
        monkey.setattr(_base, "_default", fresh)
        try:
            register_builtin()
            parsers = list(fresh.parsers())
            assert len(parsers) == 10
            names = {p.name for p in parsers}
            assert names == {
                "markdown", "html", "code", "log",
                "txt", "pdf", "docx", "xlsx", "pptx", "ocr",
            }
            # 二次调用幂等
            register_builtin()
            assert len(list(fresh.parsers())) == 10
        finally:
            monkey.undo()

        # 幂等性：连续调用 register_builtin() 不增加全局 registry 大小
        register_builtin()
        n_after_first = len(list(registry().parsers()))
        register_builtin()
        n_after_second = len(list(registry().parsers()))
        # 二次调用后 count 不增
        assert n_after_second == n_after_first
        # 且至少有 10 个
        assert n_after_first >= 10

    def test_registry_finds_xlsx_and_pptx(self, tmp_path):
        """通过 registry().find 按扩展名能找到 XlsxParser / PptxParser。"""
        register_builtin()
        reg = registry()
        xlsx = tmp_path / "a.xlsx"
        xlsx.write_bytes(b"stub")  # 不实际解析，找 parser 即可
        pptx = tmp_path / "b.pptx"
        pptx.write_bytes(b"stub")
        p_xlsx = reg.find(xlsx)
        p_pptx = reg.find(pptx)
        assert p_xlsx is not None
        assert p_xlsx.name == "xlsx"
        assert p_pptx is not None
        assert p_pptx.name == "pptx"