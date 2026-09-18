"""分块器（P1-B / P1-C）。

策略：
- 优先按段落（\n\n）切分；每段超过 max_chars 时按窗口重叠再切
- 短段（< min_chars）会与下一段合并
- 段落不足时退化为按句号 + 窗口

默认参数：max_chars=1200, overlap=120, min_chars=80
（与 V1 INDEX 摘要 80 字约束对齐；1200 字约为 600 token，适配多数 embedding 模型）
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from ..domain.models import Chunk


_SENT_END = re.compile(r"(?<=[。！？!?；;])\s*")


@dataclass
class ChunkingConfig:
    max_chars: int = 1200
    overlap: int = 120
    min_chars: int = 80


def _split_paragraphs(text: str) -> list[str]:
    return [p.strip() for p in text.split("\n\n") if p.strip()]


def _split_sentences(text: str) -> list[str]:
    parts = _SENT_END.split(text)
    return [p.strip() for p in parts if p.strip()]


def _window(text: str, max_chars: int, overlap: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    step = max(1, max_chars - overlap)
    out: list[str] = []
    i = 0
    while i < len(text):
        out.append(text[i : i + max_chars].strip())
        if i + max_chars >= len(text):
            break
        i += step
    return [p for p in out if p]


def split_into_chunks(text: str, cfg: ChunkingConfig | None = None,
                       doc_id: str = "", version_id: str = "") -> list[Chunk]:
    """主入口：把任意文本切成 Chunk 列表。doc_id / version_id 由 service 注入。"""
    from .models_helpers import _new_chunk  # 见下；避免循环引用
    cfg = cfg or ChunkingConfig()
    paras = _split_paragraphs(text)
    if not paras:
        return []

    pieces: list[str] = []
    buf = ""
    for p in paras:
        if len(buf) + len(p) + 2 <= cfg.max_chars and len(buf) < cfg.max_chars:
            buf = (buf + "\n\n" + p).strip() if buf else p
        else:
            if buf:
                pieces.append(buf)
            if len(p) > cfg.max_chars:
                pieces.extend(_window(p, cfg.max_chars, cfg.overlap))
                buf = ""
            else:
                buf = p
    if buf:
        pieces.append(buf)

    # 过短合并
    merged: list[str] = []
    for p in pieces:
        if merged and len(merged[-1]) < cfg.min_chars:
            merged[-1] = (merged[-1] + "\n\n" + p).strip()
        else:
            merged.append(p)
    pieces = merged

    out: list[Chunk] = []
    cursor = 0
    for ord_, p in enumerate(pieces):
        idx = text.find(p, cursor)
        start = idx if idx >= 0 else cursor
        end = start + len(p)
        out.append(_new_chunk(doc_id=doc_id, version_id=version_id,
                              ordinal=ord_, text=p, start=start, end=end))
        cursor = end
    return out
