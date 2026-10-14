"""文档解析入口（M4-1）。

解析实现统一在 knowbase.parsers（registry + 内置 parser）；本模块只保留对外 API：

- ``ParsedDocument``：旧结构（text / meta / segments / format / source_path）
- ``register_parser()``：允许外部按扩展名覆盖解析行为（优先于内置 parser）
- ``detect_format()``：格式标识，供 segmenter 策略与下游展示使用
- ``parse_document()``：统一分发；内置格式委托 knowbase.parsers registry
- ``list_supported_formats()``：自定义扩展名 + 内置扩展名

新 registry 的 ``ParsedDocument`` 只含 text/meta，segments 由本模块用
extraction.segmenter 生成（registry 不负责 chunking）。
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .segmenter import Segment


@dataclass
class ParsedDocument:
    """解析后的文档结构。"""
    text: str                          # 完整文本内容
    meta: dict = field(default_factory=dict)  # 元数据（标题、作者、日期等）
    segments: list[Segment] = field(default_factory=list)  # 切分后的片段
    format: str = ""                   # 检测到的格式
    source_path: str = ""              # 源文件路径


# 解析器函数签名：(path: Path) -> ParsedDocument
ParserFunc = Callable[[Path], ParsedDocument]


# 解析器注册表：扩展名 -> 解析函数
_PARSER_REGISTRY: dict[str, ParserFunc] = {}


def register_parser(extensions: list[str], parser: ParserFunc) -> None:
    """注册解析器函数。

    Args:
        extensions: 支持的扩展名列表（如 [".md", ".markdown"]）
        parser: 解析函数
    """
    for ext in extensions:
        ext_lower = ext.lower()
        if not ext_lower.startswith("."):
            ext_lower = "." + ext_lower
        _PARSER_REGISTRY[ext_lower] = parser


def detect_format(path: Path) -> str:
    """检测文件格式。

    优先使用扩展名，如果无法识别则进行内容嗅探。

    Returns:
        格式标识符：
        - "markdown": Markdown 文档
        - "plaintext": 纯文本
        - "html": HTML 文档
        - "python": Python 代码
        - "javascript": JavaScript 代码
        - "go": Go 代码
        - "java": Java 代码
        - "pdf" / "docx" / "xlsx" / "pptx" / "image": 二进制或图片文档
        - "unknown": 无法识别
    """
    path = Path(path)
    ext = path.suffix.lower()

    # 扩展名映射
    ext_to_format = {
        ".md": "markdown",
        ".markdown": "markdown",
        ".mdx": "markdown",
        ".txt": "plaintext",
        ".text": "plaintext",
        ".html": "html",
        ".htm": "html",
        ".xhtml": "html",
        ".py": "python",
        ".pyw": "python",
        ".pyi": "python",
        ".js": "javascript",
        ".mjs": "javascript",
        ".cjs": "javascript",
        ".jsx": "javascript",
        ".ts": "typescript",
        ".tsx": "typescript",
        ".go": "go",
        ".java": "java",
        ".c": "c",
        ".h": "c",
        ".cpp": "cpp",
        ".cxx": "cpp",
        ".cc": "cpp",
        ".hpp": "cpp",
        ".hxx": "cpp",
        ".cs": "csharp",
        ".rs": "rust",
        ".rb": "ruby",
        ".php": "php",
        ".swift": "swift",
        ".kt": "kotlin",
        ".kts": "kotlin",
        ".scala": "scala",
        ".sh": "shell",
        ".bash": "shell",
        ".zsh": "shell",
        ".fish": "shell",
        ".sql": "sql",
        ".json": "json",
        ".yaml": "yaml",
        ".yml": "yaml",
        ".xml": "xml",
        ".csv": "csv",
        ".log": "log",
        ".pdf": "pdf",
        ".docx": "docx",
        ".xlsx": "xlsx",
        ".xlsm": "xlsx",
        ".pptx": "pptx",
        ".png": "image",
        ".jpg": "image",
        ".jpeg": "image",
        ".gif": "image",
        ".bmp": "image",
        ".webp": "image",
        ".tif": "image",
        ".tiff": "image",
    }

    if ext in ext_to_format:
        return ext_to_format[ext]

    # 内容嗅探（如果文件存在）
    if path.exists() and path.is_file():
        try:
            # 读取前 1024 字节进行嗅探
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read(1024)

            # HTML 检测（优先级最高）
            if content.strip().lower().startswith(("<!doctype html", "<html")):
                return "html"

            # 代码检测（优先于 Markdown，因为代码注释可能被误判为 Markdown）
            code_keywords = ["def ", "class ", "function ", "func ", "package ",
                           "import ", "from ", "require(", "module "]
            if any(keyword in content for keyword in code_keywords):
                # 尝试从 shebang 或 import 推断语言
                first_line = content.split("\n")[0] if content else ""
                if "python" in first_line.lower():
                    return "python"
                elif "node" in first_line.lower() or "javascript" in first_line.lower():
                    return "javascript"
                elif "bash" in first_line.lower() or "sh" in first_line.lower():
                    return "shell"
                # 检查是否有明显的代码特征
                if any(kw in content for kw in ["def ", "class ", "import ", "from "]):
                    return "python"
                if "function " in content or "const " in content or "let " in content:
                    return "javascript"
                if "func " in content or "package " in content:
                    return "go"

            # Markdown 检测（有标题或列表，但要排除代码注释）
            # 只检测行首的 # 而不是行内的 #
            lines = content.split("\n")[:20]
            has_markdown_heading = any(
                line.strip().startswith("#") and not line.strip().startswith("#!")
                for line in lines
            )
            has_markdown_list = any(
                line.strip().startswith(("- ", "* ", "> "))
                for line in lines
            )
            if has_markdown_heading or has_markdown_list:
                return "markdown"

            # 默认返回纯文本
            return "plaintext"
        except Exception:
            pass

    return "unknown"


# 需要按代码结构切分的格式标识（与 knowbase.parsers.code_parser 的语言集合对齐）
_CODE_FORMATS = frozenset({
    "python", "javascript", "typescript", "go", "java", "c", "cpp",
    "csharp", "rust", "ruby", "php", "swift", "kotlin", "scala", "shell", "sql",
})

# 只有 HTML 的标题来自文档内容；其余内置格式沿用旧契约：标题 = 文件名
_TITLE_FROM_CONTENT = frozenset({"html"})


def _builtin_registry():
    """内置解析器 registry（幂等注册）。"""
    from ..parsers import register_builtin, registry

    register_builtin()
    return registry()


def _builtin_extensions() -> set[str]:
    """内置 parser 声明支持的扩展名。"""
    exts: set[str] = set()

    for p in _builtin_registry().parsers():
        for ext in getattr(p, "extensions", ()):
            if ext:
                exts.add(ext.lower())

    return exts


def _segment_source_format(fmt: str) -> str:
    """按格式标识选择 segmenter 策略。"""
    if fmt in ("markdown", "html"):
        return "markdown"
    if fmt in _CODE_FORMATS:
        return "code"
    return "plaintext"


def _adapt(
    path: Path,
    fmt: str,
    parser,
) -> ParsedDocument:
    """把 knowbase.parsers 的结果适配成旧 ParsedDocument（含 segments）。"""
    from .segmenter import segment_text

    parsed = parser.parse(path)
    meta = dict(parsed.meta)
    meta.setdefault("title", path.stem)

    if parser.name not in _TITLE_FROM_CONTENT:
        meta["title"] = path.stem

    meta["parser"] = parser.name

    return ParsedDocument(
        text=parsed.text,
        meta=meta,
        segments=segment_text(
            parsed.text,
            source_format=_segment_source_format(fmt),
        ),
        format=fmt,
        source_path=str(path),
    )


def parse_document(path: Path) -> Optional[ParsedDocument]:
    """解析文档。

    分发顺序：
    1. 外部通过 register_parser() 注册的解析函数（最高优先级）
    2. knowbase.parsers 内置 registry（Markdown / HTML / 代码 / 日志 / 纯文本 /
       PDF / DOCX / XLSX / PPTX / OCR）
    3. 兜底：按 UTF-8 读取文本

    Args:
        path: 文档路径

    Returns:
        ParsedDocument 对象，如果解析失败返回 None
    """
    path = Path(path)

    if not path.exists() or not path.is_file():
        return None

    # 检测格式
    fmt = detect_format(path)

    # 1. 自定义注册表优先
    ext = path.suffix.lower()
    parser = _PARSER_REGISTRY.get(ext)

    if parser:
        try:
            doc = parser(path)
            doc.format = fmt
            doc.source_path = str(path)
            return doc
        except Exception:
            # 解析失败，返回 None
            return None

    # 2. 内置 registry
    builtin = _builtin_registry().find(path)

    if builtin is not None:
        try:
            return _adapt(path, fmt, builtin)
        except Exception:
            return None

    # 3. 兜底：纯文本读取
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            text = f.read()

        from .segmenter import segment_text

        return ParsedDocument(
            text=text,
            meta={"title": path.stem},
            segments=segment_text(
                text,
                source_format=_segment_source_format(fmt),
            ),
            format=fmt,
            source_path=str(path),
        )
    except Exception:
        return None


def list_supported_formats() -> list[str]:
    """列出所有支持的扩展名（自定义注册 + 内置 parser）。"""
    return sorted(set(_PARSER_REGISTRY.keys()) | _builtin_extensions())


# registry 没有专门 parser、但可以按纯文本兜底解析的扩展名
TEXT_FALLBACK_EXTENSIONS = frozenset({".json", ".yaml", ".yml", ".xml", ".csv"})


def importable_extensions() -> set[str]:
    """import / extract 的文件发现扩展名集合。

    = parser registry 声明的扩展名（内置 + 自定义注册）+ 纯文本兜底扩展名。
    CLI 与 MCP 共用此集合，避免各自硬编码一份而漏掉新 parser 已支持的格式。
    """
    return set(list_supported_formats()) | set(TEXT_FALLBACK_EXTENSIONS)


# ============================================================================
# HTML 兼容入口（M4-2）
# ============================================================================

def parse_html(path: Path) -> ParsedDocument:
    """解析 HTML 文档（M4-2 兼容入口）。

    实现已统一到 knowbase.parsers.HtmlParser，本函数只做旧签名与
    segments 的适配，parse_document() 也走同一个实现。
    """
    from ..parsers.html_parser import HtmlParser

    path = Path(path)
    return _adapt(path, "html", HtmlParser())
