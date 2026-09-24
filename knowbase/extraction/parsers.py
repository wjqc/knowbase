"""文档解析器注册表和格式检测。

M4-1: 统一的文档解析接口，支持多种格式（Markdown、纯文本、HTML、代码文件等）。
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
        - "unknown": 无法识别
    """
    path = Path(path)
    ext = path.suffix.lower()

    # 扩展名映射
    ext_to_format = {
        ".md": "markdown",
        ".markdown": "markdown",
        ".txt": "plaintext",
        ".text": "plaintext",
        ".html": "html",
        ".htm": "html",
        ".py": "python",
        ".pyw": "python",
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
        ".rs": "rust",
        ".rb": "ruby",
        ".php": "php",
        ".sh": "shell",
        ".bash": "shell",
        ".zsh": "shell",
        ".fish": "shell",
        ".json": "json",
        ".yaml": "yaml",
        ".yml": "yaml",
        ".xml": "xml",
        ".csv": "csv",
        ".log": "log",
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


def parse_document(path: Path) -> Optional[ParsedDocument]:
    """解析文档。

    根据文件格式分发到对应的解析器。

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

    # 查找解析器
    ext = path.suffix.lower()
    parser = _PARSER_REGISTRY.get(ext)

    if parser:
        try:
            doc = parser(path)
            doc.format = fmt
            doc.source_path = str(path)
            return doc
        except Exception as e:
            # 解析失败，返回 None
            return None

    # 没有注册的解析器，使用默认解析器（纯文本）
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            text = f.read()

        from .segmenter import segment_text

        # 根据格式选择切分策略
        if fmt == "markdown":
            segments = segment_text(text, source_format="markdown")
        elif fmt in ("python", "javascript", "go", "java", "c", "cpp", "rust", "ruby", "php"):
            segments = segment_text(text, source_format="code")
        else:
            segments = segment_text(text, source_format="plaintext")

        return ParsedDocument(
            text=text,
            meta={"title": path.stem},
            segments=segments,
            format=fmt,
            source_path=str(path),
        )
    except Exception:
        return None


def list_supported_formats() -> list[str]:
    """列出所有支持的格式。"""
    return sorted(set(_PARSER_REGISTRY.keys()))


# ============================================================================
# HTML 解析器（M4-2）
# ============================================================================

from html.parser import HTMLParser


class _HTMLContentExtractor(HTMLParser):
    """HTML 内容提取器。

    提取正文内容，去除 script/style/nav 等无关标签。
    保留标题层级作为 locator。
    """

    # 需要跳过的标签
    SKIP_TAGS = {"script", "style", "nav", "header", "footer", "noscript", "iframe"}

    # 块级标签（会产生换行）
    BLOCK_TAGS = {
        "p", "div", "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6",
        "ul", "ol", "li", "table", "tr", "td", "th", "thead", "tbody",
        "blockquote", "pre", "article", "section", "aside"
    }

    def __init__(self):
        super().__init__()
        self.result = []
        self.current_tag = ""
        self.tag_stack = []
        self.skip_depth = 0
        self.heading_stack = []  # 当前标题路径
        self.in_heading = False
        self.current_heading_level = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        self.tag_stack.append(tag)

        # 检查是否需要跳过
        if tag in self.SKIP_TAGS:
            self.skip_depth += 1
            return

        if self.skip_depth > 0:
            return

        # 处理标题
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            level = int(tag[1])
            self.in_heading = True
            self.current_heading_level = level
            # 更新标题路径
            while len(self.heading_stack) >= level:
                self.heading_stack.pop()
            self.heading_stack.append("")  # 占位，内容在 handle_data 中填充
            self.result.append(f"\n{'#' * level} ")
        elif tag in self.BLOCK_TAGS:
            self.result.append("\n")

    def handle_endtag(self, tag):
        tag = tag.lower()

        if tag in self.SKIP_TAGS and self.skip_depth > 0:
            self.skip_depth -= 1
            return

        if self.skip_depth > 0:
            return

        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self.in_heading = False
            self.result.append("\n")
        elif tag in self.BLOCK_TAGS:
            self.result.append("\n")

        if self.tag_stack and self.tag_stack[-1] == tag:
            self.tag_stack.pop()

    def handle_data(self, data):
        if self.skip_depth > 0:
            return

        text = data.strip()
        if not text:
            return

        # 更新当前标题
        if self.in_heading and self.heading_stack:
            self.heading_stack[-1] = text

        self.result.append(text)

    def get_text(self) -> str:
        """获取提取的文本。"""
        text = "".join(self.result)
        # 清理多余的换行
        import re
        text = re.sub(r'\n{3,}', '\n\n', text)
        return text.strip()


def parse_html(path: Path) -> ParsedDocument:
    """解析 HTML 文档（M4-2）。

    - 提取正文内容（去除 script/style/nav）
    - 保留标题层级
    - 表格提取为结构化文本
    """
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        content = f.read()

    extractor = _HTMLContentExtractor()
    extractor.feed(content)
    text = extractor.get_text()

    # 提取标题
    title = ""
    import re
    title_match = re.search(r'<title[^>]*>(.*?)</title>', content, re.IGNORECASE | re.DOTALL)
    if title_match:
        title = title_match.group(1).strip()
    else:
        # 尝试从 h1 提取
        h1_match = re.search(r'<h1[^>]*>(.*?)</h1>', content, re.IGNORECASE | re.DOTALL)
        if h1_match:
            title = re.sub(r'<[^>]+>', '', h1_match.group(1)).strip()
        else:
            title = path.stem

    # 切分
    from .segmenter import segment_text
    segments = segment_text(text, source_format="markdown")

    return ParsedDocument(
        text=text,
        meta={"title": title},
        segments=segments,
    )


# 注册 HTML 解析器
register_parser([".html", ".htm"], parse_html)
