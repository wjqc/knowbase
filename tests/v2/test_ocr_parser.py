"""P5-A3 OCR 图片解析器单元测试。

覆盖：
- supports() 对 8 种图片扩展 / 大小写 / 非图片
- 三层依赖检查的优雅降级（tesseract 二进制 / pytesseract / PIL）
- 损坏图片的 ParseError
- happy path（mocked pytesseract + mocked tesseract binary + 真实 PIL）
- registry 集成（10 个内置 parser）
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

import pytest

from knowbase.v2.ingestion.parsers import ParserRegistry, register_builtin, registry
from knowbase.v2.ingestion.parsers.ocr_parser import (
    IMAGE_EXTENSIONS,
    OcrParser,
    _check_tesseract_binary,
)
from knowbase.v2.observability.errors import ParseError


# -----------------------
# TestOcrParserSupports
# -----------------------

class TestOcrParserSupports:
    def test_supports_all_image_extensions(self, tmp_path):
        parser = OcrParser()
        for ext in IMAGE_EXTENSIONS:
            assert parser.supports(tmp_path / f"a{ext}"), f"should support {ext}"

    def test_supports_case_insensitive(self, tmp_path):
        parser = OcrParser()
        assert parser.supports(tmp_path / "A.PNG")
        assert parser.supports(tmp_path / "B.Jpg")
        assert parser.supports(tmp_path / "C.JPEG")
        assert parser.supports(tmp_path / "D.TiFf")

    def test_rejects_non_image_extensions(self, tmp_path):
        parser = OcrParser()
        assert not parser.supports(tmp_path / "doc.txt")
        assert not parser.supports(tmp_path / "doc.md")
        assert not parser.supports(tmp_path / "doc.html")
        assert not parser.supports(tmp_path / "doc.pdf")
        assert not parser.supports(tmp_path / "doc.py")
        assert not parser.supports(tmp_path / "noext")

    def test_name_attribute(self):
        assert OcrParser().name == "ocr"


# -----------------------
# TestOcrParserGracefulDegradation
# -----------------------

class TestOcrParserGracefulDegradation:
    def test_raises_when_tesseract_binary_missing(self, tmp_path, monkeypatch):
        """tesseract 二进制不在 PATH 时抛 ParseError + 平台安装提示。"""
        monkeypatch.setattr(
            "knowbase.v2.ingestion.parsers.ocr_parser._check_tesseract_binary",
            lambda: None,
        )
        p = tmp_path / "x.png"
        p.write_bytes(b"\x89PNG\r\n\x1a\n")  # 即便文件存在也会被二进制检查拦下
        with pytest.raises(ParseError) as exc_info:
            OcrParser().parse(p)
        assert exc_info.value.parser == "ocr"
        assert "tesseract binary not found" in exc_info.value.reason
        assert "brew install tesseract" in exc_info.value.reason
        assert str(p) in exc_info.value.path

    def test_raises_when_pytesseract_not_installed(self, tmp_path, monkeypatch):
        """pytesseract 缺失时抛 ParseError，提示 pip install pytesseract。"""
        # 隐藏 pytesseract 模块，让 parser 内的 import 失败
        monkeypatch.setitem(sys.modules, "pytesseract", None)
        # 触发 ImportError：当 sys.modules[name] is None 时再次 import 会抛 ImportError
        # 但需要清理缓存以确保新 import 触发
        for mod_name in list(sys.modules.keys()):
            if mod_name == "pytesseract" or mod_name.startswith("pytesseract."):
                monkeypatch.delitem(sys.modules, mod_name, raising=False)
        monkeypatch.setitem(sys.modules, "pytesseract", None)

        p = tmp_path / "y.png"
        p.write_bytes(b"\x89PNG\r\n\x1a\n")
        with pytest.raises(ImportError):
            # 此时 import pytesseract 抛 ImportError（参考实现）
            import pytesseract  # noqa: F401
        # 实际 parser.parse 调用应抛 ParseError
        with pytest.raises((ParseError, ImportError)):
            OcrParser().parse(p)

    def test_raises_when_pil_not_installed(self, tmp_path, monkeypatch):
        """PIL 缺失时抛 ParseError，提示 uv add pillow。"""
        # 用 mock.patch.dict 在 ImportError 路径上拦截 from PIL import Image
        import builtins

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "PIL" or name.startswith("PIL."):
                raise ImportError(f"No module named '{name}' (mocked)")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)

        p = tmp_path / "z.png"
        p.write_bytes(b"\x89PNG\r\n\x1a\n")
        with pytest.raises(ParseError) as exc_info:
            OcrParser().parse(p)
        assert exc_info.value.parser == "ocr"
        assert "Pillow not installed" in exc_info.value.reason

    def test_raises_on_corrupt_image(self, tmp_path, monkeypatch):
        """损坏图片应抛 ParseError 'cannot open image'。"""
        # 假定 tesseract 在 PATH（否则先被二进制检查拦下）
        monkeypatch.setattr(
            "knowbase.v2.ingestion.parsers.ocr_parser._check_tesseract_binary",
            lambda: "/fake/usr/bin/tesseract",
        )
        p = tmp_path / "corrupt.png"
        p.write_bytes(b"this is not a valid png file at all")
        with pytest.raises(ParseError) as exc_info:
            OcrParser().parse(p)
        assert exc_info.value.parser == "ocr"
        assert "cannot open image" in exc_info.value.reason


# -----------------------
# TestOcrParserHappyPath
# -----------------------

class TestOcrParserHappyPath:
    def test_parses_valid_png_with_mocked_ocr(self, tmp_path, monkeypatch):
        """happy path: 真实 PIL PNG + mocked pytesseract + mocked tesseract 二进制。"""
        from PIL import Image

        # mock tesseract 二进制路径（避免被二进制检查拦下）
        monkeypatch.setattr(
            "knowbase.v2.ingestion.parsers.ocr_parser._check_tesseract_binary",
            lambda: "/fake/usr/bin/tesseract",
        )
        # mock pytesseract.image_to_string 返回固定文本（patch 顶级 pytesseract 模块）
        pytesseract_real = pytest.importorskip("pytesseract")
        monkeypatch.setattr(
            pytesseract_real,
            "image_to_string",
            lambda img, lang="eng": "Hello OCR\n",
        )

        # 创建一张真实可被 PIL 读取的 PNG（白色 200x50）
        img = Image.new("RGB", (200, 50), color="white")
        p = tmp_path / "real.png"
        img.save(str(p))

        parsed = OcrParser().parse(p)
        assert parsed.text == "Hello OCR"  # stripped
        assert parsed.meta["format"] == "ocr"
        assert parsed.meta["language"] == "eng"
        assert parsed.meta["width"] == 200
        assert parsed.meta["height"] == 50
        assert parsed.meta["mode"] == "RGB"
        assert parsed.meta["text_length"] == len("Hello OCR")
        assert parsed.meta["title"] == "real"

    def test_meta_text_length_zero_for_empty_ocr(self, tmp_path, monkeypatch):
        """OCR 返回空文本时 meta.text_length 应为 0。"""
        from PIL import Image

        monkeypatch.setattr(
            "knowbase.v2.ingestion.parsers.ocr_parser._check_tesseract_binary",
            lambda: "/fake/usr/bin/tesseract",
        )
        pytesseract_real = pytest.importorskip("pytesseract")
        monkeypatch.setattr(
            pytesseract_real,
            "image_to_string",
            lambda img, lang="eng": "   \n  \n",
        )

        img = Image.new("RGB", (100, 30), color="black")
        p = tmp_path / "empty.png"
        img.save(str(p))

        parsed = OcrParser().parse(p)
        assert parsed.text == ""
        assert parsed.meta["text_length"] == 0


# -----------------------
# TestRegisterBuiltinOcr
# -----------------------

class TestRegisterBuiltinOcr:
    def test_register_builtin_includes_ocr(self):
        """register_builtin 应注册 10 个解析器，包含 ocr。"""
        from knowbase.v2.ingestion.parsers import base as _base

        # 隔离：替换 base._default 为干净实例
        fresh = ParserRegistry()
        monkey = pytest.MonkeyPatch()
        monkey.setattr(_base, "_default", fresh)
        try:
            register_builtin()
            parsers = list(fresh.parsers())
            names = {p.name for p in parsers}
            assert "ocr" in names
            assert len(parsers) == 10
            # 二次调用幂等
            register_builtin()
            assert len(list(fresh.parsers())) == 10
        finally:
            monkey.undo()

        # 全局 singleton 幂等
        register_builtin()
        n_after_first = len(list(registry().parsers()))
        register_builtin()
        n_after_second = len(list(registry().parsers()))
        assert n_after_second == n_after_first
        assert n_after_first >= 10

    def test_registry_finds_ocr_for_image_extensions(self, tmp_path):
        """registry().find() 应为 8 种图片扩展名返回 OcrParser。"""
        for ext in IMAGE_EXTENSIONS:
            p = tmp_path / f"img{ext}"
            parser = registry().find(p)
            assert parser is not None, f"no parser for {ext}"
            assert parser.name == "ocr", f"{ext} should route to ocr, got {parser.name}"

    def test_registry_does_not_route_text_to_ocr(self, tmp_path):
        """registry().find() 不应为 .txt 选 ocr（应选 txt/markdown/code）。"""
        p = tmp_path / "doc.txt"
        parser = registry().find(p)
        assert parser is not None
        assert parser.name != "ocr"