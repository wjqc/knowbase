"""Parser golden set (P5-C1).

Each `ParserCase` pins a parser's behavior on a fixed input:
- input_text: the bytes to parse
- extension: file extension (e.g. ".md") used to drive `registry().find()`
- expected_meta_keys: subset that MUST appear in parsed.meta (helps catch
  accidental meta key rename / removal during refactors)
- expected_meta_contains: dict of substring checks against meta values
- expected_headings: tuple of heading strings (markdown only; ignored for
  parsers that don't emit headings)
- expected_text_contains: substring that MUST appear in parsed.text
- expected_text_excludes: substring that MUST NOT appear (e.g. "```" fences
  for parsers that strip them)

Usage:
    cases = load_default_parser_golden_set()
    runner.run_parser_golden_set(cases)

Why pin parser output instead of writing more parser-specific tests:
- A single regression in markdown stripping (e.g. accidentally treating \r
  as \n in a regression) breaks dozens of parser-specific tests; pinning
  expected meta + key text fragments catches it in one place.
- Same case set can be reused across parser versions for diff comparison.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class ParserCase:
    case_id: str                  # short unique id, e.g. "md_simple"
    parser_name: str              # must equal DocumentParser.name
    extension: str                # ".md" / ".py" / ".html" / ...
    input_text: str               # raw input
    expected_meta_keys: tuple[str, ...] = ()
    expected_meta_contains: tuple[tuple[str, str], ...] = ()  # (key, substring)
    expected_headings: tuple[str, ...] = ()    # markdown-only
    expected_text_contains: tuple[str, ...] = ()
    expected_text_excludes: tuple[str, ...] = ()
    note: str = ""


@dataclass
class ParserGoldenSet:
    name: str
    version: int
    cases: list[ParserCase] = field(default_factory=list)

    def add(self, case: ParserCase) -> None:
        self.cases.append(case)

    def extend(self, cases: Iterable[ParserCase]) -> None:
        self.cases.extend(cases)

    def by_parser(self, parser_name: str) -> list[ParserCase]:
        return [c for c in self.cases if c.parser_name == parser_name]

    def __len__(self) -> int:
        return len(self.cases)


# ---------- 默认 fixture ----------


_MD_SIMPLE = """# 标题

第一段正文包含 中文。

## 二级标题

- 列表项 1
- 列表项 2

```python
def hello():
    pass
```
"""

_MD_NO_HEADING = """无标题正文，纯段落。

第二段继续。
"""

_MD_FORMATTING = """# T1

**bold** and *italic* and `inline code`.

> blockquote
"""

_PY_SIMPLE = '''"""module docstring."""
import os

class Foo:
    """Foo class."""
    def bar(self):
        return 1

def top_level(x: int) -> int:
    return x + 1
'''

_HTML_SIMPLE = """<!doctype html>
<html><head><title>示例</title></head>
<body>
<h1>主标题</h1>
<p>正文段落包含 <b>bold</b> 和 <a href="x">链接</a>。</p>
<script>alert('x')</script>
</body></html>
"""

_CODE_JS_SIMPLE = """function greet(name) {
    return 'Hello ' + name;
}

export class UserService {
    async findOne(id) {
        return null;
    }
}
"""

_LOG_SIMPLE = """2026-09-18 10:00:00 INFO startup complete
2026-09-18 10:00:01 ERROR connection refused: 127.0.0.1:6379
2026-09-18 10:00:02 WARN retry attempt 1/3
"""


def load_default_parser_golden_set() -> ParserGoldenSet:
    """Default parser golden set covering markdown / code / html / log."""
    return ParserGoldenSet(
        name="knowbase-parsers-default",
        version=1,
        cases=[
            # ----- markdown -----
            ParserCase(
                case_id="md_simple",
                parser_name="markdown",
                extension=".md",
                input_text=_MD_SIMPLE,
                expected_meta_keys=("title", "headings", "format"),
                expected_meta_contains=(("format", "markdown"),),
                expected_headings=("标题", "二级标题"),
                expected_text_contains=("中文", "列表项 1"),
                expected_text_excludes=(),
                note="基本 markdown：标题 + 段落 + 列表 + 代码块",
            ),
            ParserCase(
                case_id="md_no_heading",
                parser_name="markdown",
                extension=".md",
                input_text=_MD_NO_HEADING,
                expected_meta_keys=("title", "headings", "format"),
                expected_meta_contains=(("headings", ""),),  # 空 tuple str repr
                expected_text_contains=("无标题正文",),
            ),
            ParserCase(
                case_id="md_formatting",
                parser_name="markdown",
                extension=".md",
                input_text=_MD_FORMATTING,
                expected_meta_keys=("title", "headings", "format"),
                expected_text_contains=("bold", "italic", "inline code"),
            ),
            # ----- python (code) -----
            ParserCase(
                case_id="py_simple",
                parser_name="code",
                extension=".py",
                input_text=_PY_SIMPLE,
                expected_meta_keys=("format", "language", "line_count",
                                   "top_level_count", "top_level_names"),
                expected_meta_contains=(("language", "python"),),
                expected_text_contains=("class Foo", "def bar", "def top_level"),
                note="ast 提取 1 class + 2 def",
            ),
            # ----- javascript (code) -----
            ParserCase(
                case_id="js_simple",
                parser_name="code",
                extension=".js",
                input_text=_CODE_JS_SIMPLE,
                expected_meta_keys=("format", "language"),
                expected_meta_contains=(("language", "javascript"),),
                expected_text_contains=("function greet", "class UserService"),
            ),
            # ----- html -----
            ParserCase(
                case_id="html_simple",
                parser_name="html",
                extension=".html",
                input_text=_HTML_SIMPLE,
                expected_meta_keys=("format", "title"),
                expected_meta_contains=(("format", "html"),),
                expected_text_contains=("主标题", "正文段落"),
                expected_text_excludes=("<script>", "alert("),
                note="script 应被剥离；纯文本保留",
            ),
            # ----- log -----
            ParserCase(
                case_id="log_simple",
                parser_name="log",
                extension=".log",
                input_text=_LOG_SIMPLE,
                expected_meta_keys=("format", "line_count"),
                expected_meta_contains=(("format", "log"),),
                expected_text_contains=("INFO", "ERROR", "WARN"),
            ),
        ],
    )


__all__ = ["ParserCase", "ParserGoldenSet", "load_default_parser_golden_set"]
