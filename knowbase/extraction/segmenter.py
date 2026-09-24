"""文本切分：按章节/段落边界切分，保留定位信息。

切分遵守章节、表格标题和上下文边界；
跨块结论必须包含多个来源片段。
"""

import re
from dataclasses import dataclass, field
from typing import NamedTuple


class Locator(NamedTuple):
    """文本定位信息。"""
    type: str          # "heading_path" | "page" | "paragraph" | "line"
    value: str         # 定位值（如 "## 第二章/### 2.1" 或 "42"）
    start_line: int = 0
    end_line: int = 0


@dataclass
class Segment:
    """切分后的文本片段。"""
    segment_id: str
    locator: Locator
    text: str
    text_hash: str = ""
    meta: dict = field(default_factory=dict)


def segment_text(text: str, *, max_chars: int = 4000, overlap: int = 200,
                 source_format: str = "markdown") -> list[Segment]:
    """将文本按结构边界切分为片段。

    - Markdown: 按 ## 标题切分
    - Code: 按函数/类定义切分（M4-3）
    - 其他: 按段落（双换行）切分
    - 超长片段二次切分（尊重 max_chars）
    - 相邻片段保留 overlap 字符重叠
    """
    if not text.strip():
        return []

    if source_format in ("markdown", "md"):
        raw_segments = _split_by_headings(text)
    elif source_format == "code":
        raw_segments = _split_by_symbols(text)
    else:
        raw_segments = _split_by_paragraphs(text)

    result = []
    for i, (locator, seg_text) in enumerate(raw_segments):
        if len(seg_text) <= max_chars:
            result.append(Segment(
                segment_id=f"seg-{i:04d}",
                locator=locator,
                text=seg_text,
            ))
        else:
            # 超长片段二次切分
            sub_parts = _split_long_text(seg_text, max_chars, overlap)
            for j, part in enumerate(sub_parts):
                result.append(Segment(
                    segment_id=f"seg-{i:04d}-{j:02d}",
                    locator=locator,
                    text=part,
                ))

    # 计算 text_hash
    import hashlib
    for seg in result:
        seg.text_hash = hashlib.sha256(seg.text.encode("utf-8")).hexdigest()[:16]

    return result


def _split_by_headings(text: str) -> list[tuple[Locator, str]]:
    """按 Markdown 标题切分。"""
    lines = text.split("\n")
    segments = []
    current_heading = ""
    current_lines: list[str] = []
    current_level = 0
    heading_path: list[str] = []
    start_line = 1

    for i, line in enumerate(lines, 1):
        m = re.match(r'^(#{1,6})\s+(.+)$', line)
        if m:
            # 保存前一段
            if current_lines:
                body = "\n".join(current_lines).strip()
                if body:
                    loc = Locator("heading_path", current_heading or "preamble",
                                  start_line, i - 1)
                    segments.append((loc, body))

            level = len(m.group(1))
            heading_text = m.group(2).strip()
            current_heading = heading_text
            current_level = level
            current_lines = [line]
            start_line = i

            # 更新标题路径
            while len(heading_path) >= level:
                heading_path.pop()
            heading_path.append(heading_text)
            current_heading = "/".join(heading_path)
        else:
            current_lines.append(line)

    # 最后一段
    if current_lines:
        body = "\n".join(current_lines).strip()
        if body:
            loc = Locator("heading_path", current_heading or "preamble",
                          start_line, len(lines))
            segments.append((loc, body))

    if not segments:
        # 无标题，整段返回
        loc = Locator("line", "1-{}".format(len(lines)), 1, len(lines))
        segments.append((loc, text.strip()))

    return segments


def _split_by_paragraphs(text: str) -> list[tuple[Locator, str]]:
    """按段落（双换行）切分。"""
    paragraphs = re.split(r'\n\s*\n', text)
    segments = []
    line_offset = 1
    for i, para in enumerate(paragraphs):
        para = para.strip()
        if not para:
            line_offset += para.count("\n") + 2
            continue
        para_lines = para.count("\n") + 1
        loc = Locator("paragraph", str(i + 1), line_offset, line_offset + para_lines - 1)
        segments.append((loc, para))
        line_offset += para_lines + 1  # +1 for blank line

    return segments


def _split_long_text(text: str, max_chars: int, overlap: int) -> list[str]:
    """将超长文本按字符限制切分，保留重叠。"""
    parts = []
    start = 0
    while start < len(text):
        end = start + max_chars
        if end >= len(text):
            parts.append(text[start:])
            break
        # 尝试在段落边界截断
        cut = text.rfind("\n\n", start + max_chars // 2, end)
        if cut < 0:
            cut = end
        parts.append(text[start:cut])
        start = cut - overlap if cut > overlap else cut
    return [p for p in parts if p.strip()]


def _split_by_symbols(text: str) -> list[tuple[Locator, str]]:
    """按代码符号（函数/类）定义切分（M4-3）。

    支持的语言关键字：
    - Python: def, class
    - JavaScript/TypeScript: function, class, const, let, var (arrow functions)
    - Go: func, type
    - Java: public, private, protected, class, interface
    - 通用: 按段落切分作为 fallback
    """
    lines = text.split("\n")
    segments = []
    current_symbol = ""
    current_lines: list[str] = []
    start_line = 1
    symbol_index = 0

    # 符号定义模式（多语言支持）
    symbol_patterns = [
        # Python: def func_name, class ClassName
        r'^\s*(?:async\s+)?def\s+(\w+)',
        r'^\s*class\s+(\w+)',
        # JavaScript/TypeScript: function funcName, class ClassName
        r'^\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)',
        r'^\s*(?:export\s+)?class\s+(\w+)',
        # Go: func FuncName, type TypeName
        r'^\s*func\s+(?:\([^)]+\)\s+)?(\w+)',
        r'^\s*type\s+(\w+)',
        # Java: public/private/protected class/interface/method
        r'^\s*(?:public|private|protected)\s+(?:static\s+)?(?:class|interface|void|int|String|boolean)\s+(\w+)',
        # Rust: fn func_name, struct StructName, impl TypeName
        r'^\s*(?:pub\s+)?fn\s+(\w+)',
        r'^\s*(?:pub\s+)?struct\s+(\w+)',
        r'^\s*impl\s+(?:<[^>]+>\s+)?(\w+)',
    ]

    def detect_symbol(line: str) -> str:
        """检测行是否包含符号定义，返回符号名或空字符串。"""
        for pattern in symbol_patterns:
            m = re.match(pattern, line)
            if m:
                return m.group(1)
        return ""

    for i, line in enumerate(lines, 1):
        symbol = detect_symbol(line)

        if symbol:
            # 保存前一段
            if current_lines:
                body = "\n".join(current_lines).strip()
                if body:
                    loc = Locator("symbol", current_symbol or f"block-{symbol_index}",
                                  start_line, i - 1)
                    segments.append((loc, body))
                    symbol_index += 1

            current_symbol = symbol
            current_lines = [line]
            start_line = i
        else:
            current_lines.append(line)

    # 最后一段
    if current_lines:
        body = "\n".join(current_lines).strip()
        if body:
            loc = Locator("symbol", current_symbol or f"block-{symbol_index}",
                          start_line, len(lines))
            segments.append((loc, body))

    if not segments:
        # 无符号定义，按段落切分
        return _split_by_paragraphs(text)

    return segments
