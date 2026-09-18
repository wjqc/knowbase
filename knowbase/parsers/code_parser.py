"""代码解析器（P5-A2）。

按文件扩展名识别语言：
- Python：用 ast 提取顶层 def / class / async / import
- 其他语言：通用正则提取 def / class / function / fn / func / struct 等顶层结构

text：原文（去掉首尾空白）
meta：format / language / line_count / top_level_count / top_level_names（前 20）
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

from .base import DocumentParser, ParsedDocument


# 扩展名 → 语言名（取自 git linguist / GitHub 常见约定）
CODE_EXTENSIONS: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".go": "go",
    ".java": "java",
    ".rs": "rust",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".hxx": "cpp",
    ".cs": "csharp",
    ".rb": "ruby",
    ".php": "php",
    ".swift": "swift",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".scala": "scala",
    ".sh": "shell",
    ".bash": "shell",
    ".zsh": "shell",
    ".sql": "sql",
}


# 通用正则：跨多语言识别顶层结构
# - 命中顺序：先 class/struct，再 def/function，再 import/from
_GENERIC_TOP_LEVEL_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Python 风格
    (re.compile(r"^(?:async\s+)?def\s+([a-zA-Z_]\w*)"), "def"),
    (re.compile(r"^class\s+([A-Z]\w*)"), "class"),
    # JS / TS
    (re.compile(r"^(?:export\s+(?:default\s+)?)?(?:async\s+)?function\s+([a-zA-Z_$][\w$]*)"), "function"),
    (re.compile(r"^(?:export\s+(?:default\s+)?)?class\s+([A-Z]\w*)"), "class"),
    (re.compile(r"^(?:export\s+(?:default\s+)?)?(?:const|let|var)\s+([a-zA-Z_$][\w$]*)\s*=\s*(?:async\s+)?\("), "const"),
    # Go
    (re.compile(r"^func\s+(?:\([^)]*\)\s+)?([a-zA-Z_]\w*)"), "func"),
    (re.compile(r"^type\s+([A-Z]\w*)\s+struct"), "struct"),
    (re.compile(r"^type\s+([A-Z]\w*)\s+interface"), "interface"),
    # Rust
    (re.compile(r"^(?:pub\s+)?(?:async\s+)?(?:unsafe\s+)?fn\s+([a-zA-Z_]\w*)"), "fn"),
    (re.compile(r"^(?:pub\s+)?struct\s+([A-Z]\w*)"), "struct"),
    (re.compile(r"^(?:pub\s+)?(?:trait|enum)\s+([A-Z]\w*)"), "trait"),
    # Ruby
    (re.compile(r"^def\s+([a-zA-Z_]\w*[?!]?)"), "def"),
    # Shell
    (re.compile(r"^(?:function\s+)?([a-zA-Z_]\w*)\s*\(\s*\)"), "function"),
    # SQL
    (re.compile(r"^(?:CREATE|REPLACE|DROP|ALTER)\s+(?:OR\s+REPLACE\s+)?(?:FUNCTION|PROCEDURE|TABLE|VIEW|INDEX|TRIGGER)\s+(?:IF\s+NOT\s+EXISTS\s+)?([a-zA-Z_]\w*)"), "sql"),
]


def _extract_python_top_level(raw: str) -> list[dict[str, object]]:
    """用 ast 提取 Python 顶层 def / class / import / from。"""
    try:
        tree = ast.parse(raw)
    except SyntaxError:
        return []
    results: list[dict[str, object]] = []
    for node in tree.body:
        if isinstance(node, ast.AsyncFunctionDef):
            results.append({"kind": "async def", "name": node.name, "line": node.lineno})
        elif isinstance(node, ast.FunctionDef):
            results.append({"kind": "def", "name": node.name, "line": node.lineno})
        elif isinstance(node, ast.ClassDef):
            results.append({"kind": "class", "name": node.name, "line": node.lineno})
        elif isinstance(node, ast.Import):
            for alias in node.names:
                results.append({"kind": "import", "name": alias.name, "line": node.lineno})
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                results.append({"kind": "from", "name": f"{module}.{alias.name}", "line": node.lineno})
    return results


def _extract_generic_top_level(raw: str) -> list[dict[str, object]]:
    """通用正则提取顶层结构。"""
    results: list[dict[str, object]] = []
    for i, line in enumerate(raw.splitlines(), start=1):
        # 跳过缩进行（顶层的定义应在 0 缩进）
        if line.startswith((" ", "\t")):
            continue
        # 跳过注释行
        stripped = line.lstrip()
        if stripped.startswith(("#", "//", "/*", "*", "--")):
            continue
        for pat, kind in _GENERIC_TOP_LEVEL_PATTERNS:
            m = pat.match(line)
            if m:
                results.append({"kind": kind, "name": m.group(1), "line": i})
                break
    return results


class CodeParser(DocumentParser):
    name = "code"

    def supports(self, path: Path) -> bool:
        return path.suffix.lower() in CODE_EXTENSIONS

    def parse(self, path: Path) -> ParsedDocument:
        from .errors import ParseError
        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            raise ParseError(parser=self.name, reason=str(e), path=str(path)) from e

        ext = path.suffix.lower()
        language = CODE_EXTENSIONS.get(ext, ext.lstrip("."))
        if ext in (".py", ".pyi"):
            top_level = _extract_python_top_level(raw)
        else:
            top_level = _extract_generic_top_level(raw)

        line_count = raw.count("\n") + (0 if raw.endswith("\n") else 1)
        text = raw.strip("\n")

        return ParsedDocument(
            text=text,
            meta={
                "format": "code",
                "language": language,
                "line_count": line_count,
                "top_level_count": len(top_level),
                "top_level_names": [t["name"] for t in top_level[:20]],
            },
        )