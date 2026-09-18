"""P5-A2 HTML / Code / Log 解析器单元测试。"""
from __future__ import annotations

from pathlib import Path

import pytest

from knowbase.parsers import register_builtin, registry
from knowbase.parsers.code_parser import (
    CODE_EXTENSIONS,
    CodeParser,
    _extract_generic_top_level,
    _extract_python_top_level,
)
from knowbase.parsers.html_parser import HtmlParser
from knowbase.parsers.log_parser import (
    _detect_format,
    _extract_level,
    _split_message,
    LogParser,
)
from knowbase.parsers.errors import ParseError


# -----------------------
# HtmlParser
# -----------------------

class TestHtmlParser:
    def test_extracts_text_and_strips_script_style(self, tmp_path):
        p = tmp_path / "page.html"
        p.write_text(
            "<html><head><title>T</title>"
            "<script>alert('x')</script>"
            "<style>.a { color: red; }</style>"
            "</head><body><h1>Hello</h1><p>World</p></body></html>",
            encoding="utf-8",
        )
        parsed = HtmlParser().parse(p)
        assert "Hello" in parsed.text
        assert "World" in parsed.text
        assert "alert" not in parsed.text  # script content 去掉
        assert ".a { color" not in parsed.text  # style content 去掉
        assert parsed.meta["title"] == "T"

    def test_preserves_heading_levels(self, tmp_path):
        p = tmp_path / "headings.html"
        p.write_text(
            "<h1>H1</h1><h2>H2</h2><h3>H3</h3><h6>H6</h6>",
            encoding="utf-8",
        )
        parsed = HtmlParser().parse(p)
        assert "# H1" in parsed.text
        assert "## H2" in parsed.text
        assert "### H3" in parsed.text
        assert "###### H6" in parsed.text

    def test_extracts_meta_description(self, tmp_path):
        p = tmp_path / "desc.html"
        p.write_text(
            '<html><head><meta name="description" content="My desc">'
            "</head><body></body></html>",
            encoding="utf-8",
        )
        parsed = HtmlParser().parse(p)
        assert parsed.meta["description"] == "My desc"

    def test_extracts_links(self, tmp_path):
        p = tmp_path / "links.html"
        p.write_text(
            '<a href="https://a.example">A</a>'
            '<a href="https://b.example">B</a>',
            encoding="utf-8",
        )
        parsed = HtmlParser().parse(p)
        assert parsed.meta["link_count"] == 2
        assert parsed.meta["link_sample"][0]["href"] == "https://a.example"

    def test_collapses_excessive_newlines(self, tmp_path):
        p = tmp_path / "n.html"
        p.write_text("<p>a</p>\n\n\n\n<p>b</p>", encoding="utf-8")
        parsed = HtmlParser().parse(p)
        # 折叠 3+ 换行为 \n\n
        assert "\n\n\n" not in parsed.text
        assert "a" in parsed.text and "b" in parsed.text

    def test_falls_back_to_stem_for_missing_title(self, tmp_path):
        p = tmp_path / "no_title.html"
        p.write_text("<p>x</p>", encoding="utf-8")
        parsed = HtmlParser().parse(p)
        assert parsed.meta["title"] == "no_title"

    def test_supports_html_extensions(self, tmp_path):
        parser = HtmlParser()
        assert parser.supports(tmp_path / "a.html")
        assert parser.supports(tmp_path / "a.htm")
        assert parser.supports(tmp_path / "a.xhtml")
        assert parser.supports(tmp_path / "A.HTML")  # 大小写不敏感
        assert not parser.supports(tmp_path / "a.txt")

    def test_handles_malformed_html_gracefully(self, tmp_path):
        """未闭合 / 异常 HTML 不应抛 ParseError（HTMLParser 本身容错）。"""
        p = tmp_path / "broken.html"
        p.write_text("<p>hello<div>world</p>", encoding="utf-8")  # 错位嵌套
        parsed = HtmlParser().parse(p)
        assert "hello" in parsed.text
        assert "world" in parsed.text


# -----------------------
# CodeParser
# -----------------------

class TestCodeParser:
    def test_python_extracts_top_level_with_ast(self, tmp_path):
        p = tmp_path / "m.py"
        p.write_text(
            "import os\n"
            "from pathlib import Path\n"
            "class MyClass:\n"
            "    def method(self):\n"  # 嵌套方法，不应出现在顶层
            "        pass\n"
            "async def fetch():\n"
            "    pass\n"
            "def regular():\n"
            "    return 1\n",
            encoding="utf-8",
        )
        parsed = CodeParser().parse(p)
        assert parsed.meta["language"] == "python"
        assert parsed.meta["format"] == "code"
        names = parsed.meta["top_level_names"]
        assert "os" in names  # import
        assert "pathlib.Path" in names  # from
        assert "MyClass" in names
        assert "fetch" in names  # async def
        assert "regular" in names
        assert "method" not in names  # 嵌套方法不应在顶层

    def test_python_handles_syntax_error(self, tmp_path):
        """SyntaxError 的 Python 文件不应抛 ParseError。"""
        p = tmp_path / "bad.py"
        p.write_text("def broken(:\n", encoding="utf-8")
        parsed = CodeParser().parse(p)
        assert parsed.meta["language"] == "python"
        # SyntaxError → 走 generic 提取（会得到 0 个）
        assert parsed.meta["top_level_count"] == 0

    def test_go_extracts_func_and_struct(self, tmp_path):
        p = tmp_path / "s.go"
        p.write_text(
            "package main\n"
            "type Server struct {\n"
            "    port int\n"
            "}\n"
            "func New() *Server {\n"
            "    return nil\n"
            "}\n"
            "func (s *Server) Start() error {\n"
            "    return nil\n"
            "}\n",
            encoding="utf-8",
        )
        parsed = CodeParser().parse(p)
        assert parsed.meta["language"] == "go"
        names = parsed.meta["top_level_names"]
        assert "Server" in names  # type struct
        assert "New" in names
        assert "Start" in names

    def test_javascript_extracts_function_and_class(self, tmp_path):
        p = tmp_path / "m.js"
        p.write_text(
            "function alpha() {}\n"
            "const beta = () => {};\n"
            "class Gamma {}\n"
            "export function delta() {}\n",
            encoding="utf-8",
        )
        parsed = CodeParser().parse(p)
        assert parsed.meta["language"] == "javascript"
        names = parsed.meta["top_level_names"]
        assert "alpha" in names
        assert "beta" in names
        assert "Gamma" in names
        assert "delta" in names

    def test_rust_extracts_fn_struct_enum(self, tmp_path):
        p = tmp_path / "l.rs"
        p.write_text(
            "pub struct Foo { x: i32 }\n"
            "pub fn bar() {}\n"
            "pub enum Baz { A, B }\n"
            "pub trait Qux { fn x(&self); }\n",
            encoding="utf-8",
        )
        parsed = CodeParser().parse(p)
        assert parsed.meta["language"] == "rust"
        names = parsed.meta["top_level_names"]
        assert "Foo" in names
        assert "bar" in names
        assert "Baz" in names
        assert "Qux" in names

    def test_skips_indented_lines(self, tmp_path):
        """缩进行不应被识别为顶层结构（避免嵌套类方法污染）。"""
        p = tmp_path / "nested.go"
        p.write_text(
            "package main\n"
            "func outer() {\n"
            "    func inner() {} // 缩进，不应是顶层\n"
        ,
            encoding="utf-8",
        )
        parsed = CodeParser().parse(p)
        names = parsed.meta["top_level_names"]
        assert "outer" in names
        assert "inner" not in names

    def test_skips_comment_lines(self, tmp_path):
        p = tmp_path / "c.go"
        p.write_text(
            "// func comment_only() {}\n"
            "# def comment_only(): pass\n"
            "func real() {}\n",
            encoding="utf-8",
        )
        parsed = CodeParser().parse(p)
        names = parsed.meta["top_level_names"]
        assert "real" in names
        assert "comment_only" not in names

    def test_supports_code_extensions(self, tmp_path):
        parser = CodeParser()
        for ext in (".py", ".js", ".ts", ".go", ".java", ".rs", ".rb"):
            assert parser.supports(tmp_path / f"x{ext}")
        assert not parser.supports(tmp_path / "x.md")
        assert not parser.supports(tmp_path / "x.txt")

    def test_code_extensions_registry_covers_common_languages(self):
        """扩展名表应覆盖至少 15 种常用语言。"""
        assert len(CODE_EXTENSIONS) >= 15
        assert CODE_EXTENSIONS[".py"] == "python"
        assert CODE_EXTENSIONS[".go"] == "go"
        assert CODE_EXTENSIONS[".rs"] == "rust"


# -----------------------
# LogParser
# -----------------------

class TestLogParser:
    def test_detects_iso8601_with_t(self):
        ts, fmt = _detect_format("2025-01-15T10:30:45.123Z INFO msg")
        assert ts == "2025-01-15T10:30:45.123Z"
        assert fmt == "iso8601"

    def test_detects_iso8601_with_space(self):
        ts, fmt = _detect_format("2025-01-15 10:30:45,123 INFO msg")
        assert ts.startswith("2025-01-15 10:30:45")
        assert fmt == "iso8601_space"

    def test_detects_syslog(self):
        ts, fmt = _detect_format("Jan 15 10:30:45 host app[123]: msg")
        assert ts.startswith("Jan 15")
        assert fmt == "syslog"

    def test_detects_common_log(self):
        ts, fmt = _detect_format("15/Jan/2025:10:30:45 +0000 GET /")
        assert ts.startswith("15/Jan/2025")
        assert fmt == "common"

    def test_detects_unix_timestamp(self):
        ts, fmt = _detect_format("1736939445 INFO msg")
        assert ts == "1736939445"
        assert fmt == "unix"

    def test_unknown_format_returns_none(self):
        ts, fmt = _detect_format("just a regular line")
        assert ts is None and fmt is None

    def test_extracts_level_aliases(self):
        assert _extract_level("INFO hello") == "INFO"
        assert _extract_level("WARN hello") == "WARN"
        assert _extract_level("WARNING hello") == "WARN"
        assert _extract_level("ERROR hello") == "ERROR"
        assert _extract_level("FATAL hello") == "FATAL"
        assert _extract_level("CRITICAL hello") == "CRITICAL"
        assert _extract_level("no level here") == ""

    def test_split_message_strips_timestamp_and_level(self):
        msg = _split_message("2025-01-15T10:30:45Z INFO Application starting", "2025-01-15T10:30:45Z", "INFO")
        assert msg == "Application starting"

    def test_parse_iso8601_log(self, tmp_path):
        p = tmp_path / "app.log"
        p.write_text(
            "2025-01-15T10:30:45.123Z INFO  App started\n"
            "2025-01-15T10:30:46.456Z WARN  Slow query\n"
            "2025-01-15T10:30:47.789Z ERROR Connection refused\n"
            "2025-01-15T10:31:00.000Z DEBUG Cache hit 0.85\n",
            encoding="utf-8",
        )
        parsed = LogParser().parse(p)
        assert parsed.meta["log_format"] == "iso8601"
        assert parsed.meta["line_count"] == 4
        assert parsed.meta["level_counts"] == {"INFO": 1, "WARN": 1, "ERROR": 1, "DEBUG": 1}
        assert parsed.meta["first_ts"] == "2025-01-15T10:30:45.123Z"
        assert parsed.meta["last_ts"] == "2025-01-15T10:31:00.000Z"
        assert "[INFO] App started" in parsed.text
        # level 不应重复出现在 msg
        assert "[INFO] INFO  App started" not in parsed.text

    def test_parse_handles_mixed_unrecognized_lines(self, tmp_path):
        """无法识别时间戳的行保留原文。"""
        p = tmp_path / "mixed.log"
        p.write_text(
            "2025-01-15T10:30:45Z INFO started\n"
            "random line without timestamp\n"
            "2025-01-15T10:30:46Z WARN done\n",
            encoding="utf-8",
        )
        parsed = LogParser().parse(p)
        assert parsed.meta["log_format"] == "iso8601"
        assert parsed.meta["recognized_count"] == 2
        assert "random line without timestamp" in parsed.text

    def test_parse_falls_back_to_plain(self, tmp_path):
        """完全无时间戳 → log_format='plain'，原文保留。"""
        p = tmp_path / "plain.log"
        p.write_text("a\nb\nc\n", encoding="utf-8")
        parsed = LogParser().parse(p)
        assert parsed.meta["log_format"] == "plain"
        assert parsed.meta["recognized_count"] == 0
        assert "a" in parsed.text and "b" in parsed.text and "c" in parsed.text

    def test_parse_syslog(self, tmp_path):
        p = tmp_path / "sys.log"
        p.write_text(
            "Jan 15 10:30:45 myhost sshd[123]: Accepted publickey for alice\n",
            encoding="utf-8",
        )
        parsed = LogParser().parse(p)
        assert parsed.meta["log_format"] == "syslog"
        assert parsed.meta["first_ts"].startswith("Jan 15")

    def test_supports_log_only(self, tmp_path):
        parser = LogParser()
        assert parser.supports(tmp_path / "a.log")
        assert parser.supports(tmp_path / "A.LOG")
        assert not parser.supports(tmp_path / "a.txt")
        assert not parser.supports(tmp_path / "a.py")


# -----------------------
# 注册表集成
# -----------------------

class TestRegisterBuiltinText:
    def test_register_builtin_includes_text_parsers(self):
        """register_builtin 应注册 10 个解析器（含 HTML / Code / Log / OCR）；幂等。"""
        from knowbase.parsers import base as _base

        fresh = __import__(
            "knowbase.parsers", fromlist=["ParserRegistry"]
        ).ParserRegistry()
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
        finally:
            monkey.undo()

    def test_log_parser_wins_over_txt_for_log_extension(self, tmp_path):
        """.log 应路由到 LogParser（而非 TxtParser）。"""
        register_builtin()
        reg = registry()
        p = tmp_path / "a.log"
        p.write_text("2025-01-15T10:30:45Z INFO test\n", encoding="utf-8")
        parser = reg.find(p)
        assert parser is not None
        assert parser.name == "log"

    def test_html_and_code_extensions_routed_correctly(self, tmp_path):
        """.html / .py 应分别路由到 HtmlParser / CodeParser。"""
        register_builtin()
        reg = registry()
        for ext, expected in ((".html", "html"), (".py", "code"), (".go", "code")):
            p = tmp_path / f"x{ext}"
            p.write_text("x", encoding="utf-8")
            parser = reg.find(p)
            assert parser is not None
            assert parser.name == expected, f"{ext} should route to {expected}"