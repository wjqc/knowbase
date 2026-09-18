"""PPTX 解析器（P5-A1）。

基于 python-pptx：
- 每个 slide 按顺序输出：slide 序号 + 全部 text_frame 段落（含 bullet）+ 备注
- slide 间用 `\n\n`；空 slide 跳过
- meta 记录 slide_count / title（core_properties）/ author（core_properties）/ title_fallback（无 core title 时用 stem）
- 缺失依赖抛 ParseError 提示 `uv add python-pptx`
"""
from __future__ import annotations

from pathlib import Path

from .base import DocumentParser, ParsedDocument


class PptxParser(DocumentParser):
    name = "pptx"

    def supports(self, path: Path) -> bool:
        return path.suffix.lower() == ".pptx"

    def parse(self, path: Path) -> ParsedDocument:
        from ...observability.errors import ParseError
        try:
            from pptx import Presentation  # type: ignore
        except ImportError as e:
            raise ParseError(parser=self.name,
                             reason="python-pptx not installed; uv add python-pptx",
                             path=str(path)) from e
        try:
            prs = Presentation(str(path))
        except Exception as e:
            raise ParseError(parser=self.name, reason=f"cannot open: {e}",
                             path=str(path)) from e

        slides_text: list[str] = []
        for i, slide in enumerate(prs.slides, start=1):
            parts: list[str] = []
            for shape in slide.shapes:
                if not shape.has_text_frame:
                    continue
                for para in shape.text_frame.paragraphs:
                    t = "".join(run.text for run in para.runs).strip()
                    if t:
                        parts.append(t)
            notes = ""
            if slide.has_notes_slide:
                notes_tf = slide.notes_slide.notes_text_frame
                if notes_tf is not None:
                    notes = notes_tf.text.strip()
            block_lines = [f"# Slide {i}"] + parts
            if notes:
                block_lines.append(f"[Notes] {notes}")
            if parts or notes:
                slides_text.append("\n".join(block_lines))

        text = "\n\n".join(slides_text).strip()

        title = ""
        author = ""
        try:
            cp = prs.core_properties
            title = (cp.title or "").strip()
            author = (cp.author or "").strip()
        except Exception:
            pass

        return ParsedDocument(
            text=text,
            meta={
                "format": "pptx",
                "title": title or path.stem,
                "slide_count": len(prs.slides),
                "author": author,
            },
        )