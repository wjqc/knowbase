"""M4 测试：多格式导入提炼。"""

import tempfile
from pathlib import Path

import pytest

from knowbase.extraction import parsers
from knowbase.extraction.parsers import ParsedDocument, detect_format, parse_document


class TestFormatDetection:
    """测试格式检测。"""

    def test_detect_markdown(self, tmp_path):
        """检测 Markdown 文件。"""
        md_file = tmp_path / "test.md"
        md_file.write_text("# Title\n\nContent")
        assert detect_format(md_file) == "markdown"

        md_file2 = tmp_path / "test.markdown"
        md_file2.write_text("# Title")
        assert detect_format(md_file2) == "markdown"

    def test_detect_plaintext(self, tmp_path):
        """检测纯文本文件。"""
        txt_file = tmp_path / "test.txt"
        txt_file.write_text("Just plain text")
        assert detect_format(txt_file) == "plaintext"

    def test_detect_html(self, tmp_path):
        """检测 HTML 文件。"""
        html_file = tmp_path / "test.html"
        html_file.write_text("<!DOCTYPE html><html><body>Test</body></html>")
        assert detect_format(html_file) == "html"

        html_file2 = tmp_path / "test.htm"
        html_file2.write_text("<html>Test</html>")
        assert detect_format(html_file2) == "html"

    def test_detect_python(self, tmp_path):
        """检测 Python 文件。"""
        py_file = tmp_path / "test.py"
        py_file.write_text("def hello():\n    print('Hello')")
        assert detect_format(py_file) == "python"

    def test_detect_javascript(self, tmp_path):
        """检测 JavaScript 文件。"""
        js_file = tmp_path / "test.js"
        js_file.write_text("function hello() { console.log('Hello'); }")
        assert detect_format(js_file) == "javascript"

    def test_detect_go(self, tmp_path):
        """检测 Go 文件。"""
        go_file = tmp_path / "test.go"
        go_file.write_text("package main\n\nfunc main() {}")
        assert detect_format(go_file) == "go"

    def test_detect_java(self, tmp_path):
        """检测 Java 文件。"""
        java_file = tmp_path / "Test.java"
        java_file.write_text("public class Test {}")
        assert detect_format(java_file) == "java"

    def test_detect_by_content_html(self, tmp_path):
        """通过内容嗅探检测 HTML。"""
        # 无扩展名但内容是 HTML
        file = tmp_path / "noext"
        file.write_text("<!DOCTYPE html><html><body>Test</body></html>")
        assert detect_format(file) == "html"

    def test_detect_by_content_markdown(self, tmp_path):
        """通过内容嗅探检测 Markdown。"""
        # 无扩展名但有 Markdown 特征
        file = tmp_path / "noext"
        file.write_text("# Title\n\n## Section\n\n- item1\n- item2")
        assert detect_format(file) == "markdown"

    def test_detect_by_content_python(self, tmp_path):
        """通过内容嗅探检测 Python。"""
        # 无扩展名但有 Python 特征
        file = tmp_path / "noext"
        file.write_text("#!/usr/bin/env python3\n\ndef hello():\n    pass")
        assert detect_format(file) == "python"

    def test_detect_unknown(self, tmp_path):
        """无法识别的格式。"""
        file = tmp_path / "test.xyz123"
        file.write_text("some content")
        # 应该回退到内容嗅探
        fmt = detect_format(file)
        assert fmt in ("plaintext", "unknown")


class TestParseDocument:
    """测试文档解析。"""

    def test_parse_markdown(self, tmp_path):
        """解析 Markdown 文档。"""
        md_file = tmp_path / "test.md"
        md_file.write_text("# Title\n\n## Section 1\n\nContent 1\n\n## Section 2\n\nContent 2")

        doc = parse_document(md_file)
        assert doc is not None
        assert doc.format == "markdown"
        assert "Title" in doc.text
        assert len(doc.segments) > 0
        assert doc.source_path == str(md_file)

    def test_parse_plaintext(self, tmp_path):
        """解析纯文本文档。"""
        txt_file = tmp_path / "test.txt"
        txt_file.write_text("Line 1\n\nLine 2\n\nLine 3")

        doc = parse_document(txt_file)
        assert doc is not None
        assert doc.format == "plaintext"
        assert "Line 1" in doc.text
        assert len(doc.segments) > 0

    def test_parse_python(self, tmp_path):
        """解析 Python 文件。"""
        py_file = tmp_path / "test.py"
        py_file.write_text("""
def hello():
    print("Hello")

def world():
    print("World")

class MyClass:
    pass
""")

        doc = parse_document(py_file)
        assert doc is not None
        assert doc.format == "python"
        assert "def hello" in doc.text
        assert len(doc.segments) > 0

    def test_parse_nonexistent(self, tmp_path):
        """解析不存在的文件。"""
        file = tmp_path / "nonexistent.txt"
        doc = parse_document(file)
        assert doc is None

    def test_parse_empty_file(self, tmp_path):
        """解析空文件。"""
        file = tmp_path / "empty.txt"
        file.write_text("")

        doc = parse_document(file)
        assert doc is not None
        assert doc.text == ""
        assert len(doc.segments) == 0

    def test_parse_with_metadata(self, tmp_path):
        """解析带元数据的文档。"""
        md_file = tmp_path / "test.md"
        md_file.write_text("# My Document\n\nContent here")

        doc = parse_document(md_file)
        assert doc is not None
        assert doc.meta.get("title") == "test"  # 默认使用文件名


class TestParsedDocument:
    """测试 ParsedDocument 数据结构。"""

    def test_parsed_document_creation(self):
        """创建 ParsedDocument。"""
        doc = ParsedDocument(
            text="Test content",
            meta={"title": "Test"},
            segments=[],
            format="plaintext",
            source_path="/path/to/file.txt"
        )
        assert doc.text == "Test content"
        assert doc.meta["title"] == "Test"
        assert doc.format == "plaintext"
        assert doc.source_path == "/path/to/file.txt"

    def test_parsed_document_defaults(self):
        """ParsedDocument 默认值。"""
        doc = ParsedDocument(text="Test")
        assert doc.meta == {}
        assert doc.segments == []
        assert doc.format == ""
        assert doc.source_path == ""


class TestParserRegistry:
    """测试解析器注册表。"""

    def test_list_supported_formats(self):
        """列出支持的格式。"""
        formats = parsers.list_supported_formats()
        assert isinstance(formats, list)
        # 应该包含一些常见格式
        # 注意：初始时注册表可能为空，这是正常的

    def test_register_custom_parser(self, tmp_path):
        """注册自定义解析器。"""
        # 定义一个简单的自定义解析器
        def custom_parser(path: Path) -> ParsedDocument:
            with open(path, "r") as f:
                text = f.read()
            return ParsedDocument(text=text, meta={"custom": True})

        # 注册
        parsers.register_parser([".custom"], custom_parser)

        # 创建测试文件
        file = tmp_path / "test.custom"
        file.write_text("Custom content")

        # 解析
        doc = parse_document(file)
        assert doc is not None
        assert doc.meta.get("custom") is True
        assert "Custom content" in doc.text


class TestHTMLParser:
    """测试 HTML 解析器（M4-2）。"""

    def test_parse_basic_html(self, tmp_path):
        """解析基本 HTML。"""
        html_file = tmp_path / "test.html"
        html_file.write_text("""
<!DOCTYPE html>
<html>
<head><title>Test Page</title></head>
<body>
<h1>Main Title</h1>
<p>This is a paragraph.</p>
<h2>Section 1</h2>
<p>Content of section 1.</p>
</body>
</html>
""")
        doc = parse_document(html_file)
        assert doc is not None
        assert doc.format == "html"
        assert doc.meta.get("title") == "Test Page"
        assert "Main Title" in doc.text
        assert "This is a paragraph" in doc.text
        assert len(doc.segments) > 0

    def test_html_skip_script_style(self, tmp_path):
        """HTML 解析应跳过 script 和 style。"""
        html_file = tmp_path / "test.html"
        html_file.write_text("""
<html>
<head>
<style>body { color: red; }</style>
<script>alert('test');</script>
</head>
<body>
<h1>Title</h1>
<p>Content</p>
</body>
</html>
""")
        doc = parse_document(html_file)
        assert doc is not None
        assert "alert" not in doc.text
        assert "color: red" not in doc.text
        assert "Title" in doc.text
        assert "Content" in doc.text

    def test_html_heading_hierarchy(self, tmp_path):
        """HTML 标题层级应正确转换。"""
        html_file = tmp_path / "test.html"
        html_file.write_text("""
<html>
<body>
<h1>Level 1</h1>
<h2>Level 2</h2>
<h3>Level 3</h3>
<p>Content</p>
</body>
</html>
""")
        doc = parse_document(html_file)
        assert doc is not None
        assert "# Level 1" in doc.text
        assert "## Level 2" in doc.text
        assert "### Level 3" in doc.text

    def test_html_table_extraction(self, tmp_path):
        """HTML 表格应提取为文本。"""
        html_file = tmp_path / "test.html"
        html_file.write_text("""
<html>
<body>
<table>
<tr><th>Name</th><th>Value</th></tr>
<tr><td>Item1</td><td>100</td></tr>
<tr><td>Item2</td><td>200</td></tr>
</table>
</body>
</html>
""")
        doc = parse_document(html_file)
        assert doc is not None
        assert "Name" in doc.text
        assert "Value" in doc.text
        assert "Item1" in doc.text
        assert "100" in doc.text

    def test_html_title_from_h1(self, tmp_path):
        """无 title 标签时从 h1 提取标题。"""
        html_file = tmp_path / "test.html"
        html_file.write_text("""
<html>
<body>
<h1>Page Title from H1</h1>
<p>Content</p>
</body>
</html>
""")
        doc = parse_document(html_file)
        assert doc is not None
        assert doc.meta.get("title") == "Page Title from H1"


# ============================================================================
# M4-6: source_ref 定位校验测试
# ============================================================================

class TestSourceRefValidation:
    """测试 source_ref 定位校验（M4-6）。"""

    def test_valid_source_ref(self):
        """有效的 source_ref 应通过校验。"""
        from knowbase.extraction.validator import validate_candidate

        candidate = {
            "body": {
                "conclusion": "测试结论",
                "problem": "测试问题",
                "scope": "test-scope",
            },
            "source_refs": [
                {
                    "source_id": "SRC-2026-0001",
                    "segment_id": "seg-0001",
                    "text_hash": "abc123def4567890",
                    "locator": {
                        "type": "heading_path",
                        "value": "## 第二章/### 2.1",
                    }
                }
            ]
        }
        result = validate_candidate(candidate, scope="test-scope")
        # 不应有 source_ref 相关的错误
        source_ref_errors = [e for e in result.errors if "source_refs" in e]
        assert len(source_ref_errors) == 0

    def test_missing_source_id(self):
        """缺少 source_id 应报错。"""
        from knowbase.extraction.validator import validate_candidate

        candidate = {
            "body": {
                "conclusion": "测试结论",
                "problem": "测试问题",
                "scope": "test-scope",
            },
            "source_refs": [
                {
                    "segment_id": "seg-0001",
                }
            ]
        }
        result = validate_candidate(candidate, scope="test-scope")
        assert any("source_id 必填" in e for e in result.errors)

    def test_invalid_source_id_format(self):
        """无效的 source_id 格式应给 warning。"""
        from knowbase.extraction.validator import validate_candidate

        candidate = {
            "body": {
                "conclusion": "测试结论",
                "problem": "测试问题",
                "scope": "test-scope",
            },
            "source_refs": [
                {
                    "source_id": "INVALID-ID",
                }
            ]
        }
        result = validate_candidate(candidate, scope="test-scope")
        assert any("格式异常" in w for w in result.warnings)

    def test_invalid_locator_type(self):
        """无效的 locator type 应给 warning。"""
        from knowbase.extraction.validator import validate_candidate

        candidate = {
            "body": {
                "conclusion": "测试结论",
                "problem": "测试问题",
                "scope": "test-scope",
            },
            "source_refs": [
                {
                    "source_id": "SRC-2026-0001",
                    "locator": {
                        "type": "invalid_type",
                    }
                }
            ]
        }
        result = validate_candidate(candidate, scope="test-scope")
        assert any("未知" in w for w in result.warnings)


# ============================================================================
# M4-7: source 版本追踪测试
# ============================================================================

class TestSourceVersioning:
    """测试 source 版本追踪（M4-7）。"""

    def test_import_versioned_first_version(self, tmp_path):
        """首次导入应创建版本 1。"""
        from knowbase import sources

        repo = tmp_path
        sources.ensure_dirs(repo)

        sid, sha, created, version = sources.import_versioned(
            repo, "测试内容",
            title="测试文档",
            scope="test-scope",
            fmt="md",
            parser_version="v1",
            imported_by="test",
            source_path="/path/to/file.md",
        )

        assert created is True
        assert version == 1
        assert sid.startswith("SRC-")

    def test_import_versioned_same_content_no_duplicate(self, tmp_path):
        """同内容不应重复创建。"""
        from knowbase import sources

        repo = tmp_path
        sources.ensure_dirs(repo)

        # 第一次导入
        sid1, sha1, created1, version1 = sources.import_versioned(
            repo, "测试内容",
            title="测试文档",
            scope="test-scope",
            fmt="md",
            parser_version="v1",
            imported_by="test",
            source_path="/path/to/file.md",
        )

        # 第二次导入（同内容）
        sid2, sha2, created2, version2 = sources.import_versioned(
            repo, "测试内容",
            title="测试文档",
            scope="test-scope",
            fmt="md",
            parser_version="v1",
            imported_by="test",
            source_path="/path/to/file.md",
        )

        assert created2 is False
        assert sid1 == sid2
        assert version1 == version2

    def test_import_versioned_new_version(self, tmp_path):
        """同路径新内容应创建新版本。"""
        from knowbase import sources

        repo = tmp_path
        sources.ensure_dirs(repo)

        # 第一次导入
        sid1, sha1, created1, version1 = sources.import_versioned(
            repo, "版本 1 内容",
            title="测试文档",
            scope="test-scope",
            fmt="md",
            parser_version="v1",
            imported_by="test",
            source_path="/path/to/file.md",
        )

        # 第二次导入（同路径，新内容）
        sid2, sha2, created2, version2 = sources.import_versioned(
            repo, "版本 2 内容",
            title="测试文档",
            scope="test-scope",
            fmt="md",
            parser_version="v1",
            imported_by="test",
            source_path="/path/to/file.md",
        )

        assert created2 is True
        assert sid1 != sid2
        assert version2 == 2

        # 检查旧版本是否被标记为 superseded
        old_manifest = sources.load_manifest(repo, sid1)
        assert old_manifest["status"] == "superseded"
        assert old_manifest["superseded_by"] == sid2

    def test_find_by_path(self, tmp_path):
        """find_by_path 应返回最新版本。"""
        from knowbase import sources

        repo = tmp_path
        sources.ensure_dirs(repo)

        # 导入版本 1
        sid1, _, _, _ = sources.import_versioned(
            repo, "版本 1",
            title="测试文档",
            scope="test-scope",
            fmt="md",
            parser_version="v1",
            imported_by="test",
            source_path="/path/to/file.md",
        )

        # 导入版本 2
        sid2, _, _, _ = sources.import_versioned(
            repo, "版本 2",
            title="测试文档",
            scope="test-scope",
            fmt="md",
            parser_version="v1",
            imported_by="test",
            source_path="/path/to/file.md",
        )

        # find_by_path 应返回最新版本
        latest = sources.find_by_path(repo, "/path/to/file.md", scope="test-scope")
        assert latest is not None
        assert latest["id"] == sid2
        assert latest["source_version"] == 2
