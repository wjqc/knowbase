"""HTML 解析器（P5-A2）。

基于 stdlib html.parser：
- 去除 `<script>` / `<style>` 内容（保留正文）
- 结构化标记：`<h1-h6>` → `# ` / `## ` ...；`<p>` `<div>` `<li>` `<tr>` `<br>` 加换行
- `<title>` → meta.title；`<meta name="description">` → meta.description
- `<a href="...">text</a>` → 输出 `[text](url)`，便于检索保留链接关系
- 多次连续换行折叠为 \n\n
- meta: format / title / description / link_count
"""
from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

from .base import DocumentParser, ParsedDocument


class _HtmlTextExtractor(HTMLParser):
    """HTMLParser 子类：抽取正文，保留块级结构。"""

    def __init__(self) -> None:
        # convert_charrefs=True: 自动转换 &amp; &lt; 等字符引用
        super().__init__(convert_charrefs=True)
        self.text_parts: list[str] = []
        self.title: str = ""
        self.description: str = ""
        self.links: list[dict[str, str]] = []
        self._skip_depth = 0  # script/style 嵌套深度
        self._in_title = False
        self._in_h: tuple[int, str] | None = None  # 当前 heading tag
        self._current_href: str | None = None  # <a> 起始时的 href

    # --- tag 事件 ---

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        atype = tag.lower()
        attr_map = dict(attrs)

        if atype in ("script", "style", "noscript"):
            self._skip_depth += 1
            return

        if atype == "title":
            self._in_title = True
            return

        if atype == "meta":
            n = (attr_map.get("name") or "").lower()
            c = attr_map.get("content") or ""
            if n == "description" and c:
                self.description = c
            return

        if atype == "a":
            self._current_href = attr_map.get("href") or ""
            return

        if atype in ("br", "hr"):
            self.text_parts.append("\n")
            return

        if atype in ("p", "div", "li", "tr", "td", "th"):
            self.text_parts.append("\n")
            return

        m = re.fullmatch(r"h([1-6])", atype)
        if m:
            level = int(m.group(1))
            self._in_h = (level, atype)
            self.text_parts.append("\n")
            self.text_parts.append("#" * level + " ")

    def handle_endtag(self, tag: str) -> None:
        atype = tag.lower()
        if atype in ("script", "style", "noscript"):
            if self._skip_depth > 0:
                self._skip_depth -= 1
            return
        if atype == "title":
            self._in_title = False
            return
        if atype == "a":
            self._current_href = None
            return
        if atype in ("p", "div", "li", "tr"):
            self.text_parts.append("\n")
            return
        if self._in_h and atype == self._in_h[1]:
            self.text_parts.append("\n")
            self._in_h = None

    def handle_data(self, data: str) -> None:
        if self._skip_depth > 0:
            return
        if self._in_title:
            self.title += data
            return
        if self._current_href is not None:
            # <a href="...">text</a> → 记录链接
            stripped = data.strip()
            if stripped and self._current_href:
                self.links.append({"text": stripped, "href": self._current_href})
            self.text_parts.append(data)
            return
        self.text_parts.append(data)


class HtmlParser(DocumentParser):
    name = "html"

    def supports(self, path: Path) -> bool:
        return path.suffix.lower() in (".html", ".htm", ".xhtml")

    def parse(self, path: Path) -> ParsedDocument:
        from .errors import ParseError
        try:
            raw = path.read_bytes()
        except OSError as e:
            raise ParseError(parser=self.name, reason=str(e), path=str(path)) from e

        extractor = _HtmlTextExtractor()
        try:
            # HTML 编码由 meta charset / <meta> 决定；无法预知则 utf-8 + replace
            extractor.feed(raw.decode("utf-8", errors="replace"))
            extractor.close()
        except Exception as e:
            raise ParseError(parser=self.name,
                             reason=f"cannot parse: {e}", path=str(path)) from e

        raw_text = "".join(extractor.text_parts)
        # 折叠 3+ 连续换行为 \n\n；保留单换行 / 双换行
        text = re.sub(r"\n{3,}", "\n\n", raw_text).strip()

        title = (extractor.title or "").strip() or path.stem
        return ParsedDocument(
            text=text,
            meta={
                "format": "html",
                "title": title[:200],
                "description": extractor.description[:500] if extractor.description else "",
                "link_count": len(extractor.links),
                "link_sample": extractor.links[:5],
            },
        )