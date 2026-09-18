"""通用小工具：避免 chunking 循环引用 domain.models。"""
from __future__ import annotations

from ..domain.models import Chunk


def _new_chunk(*, doc_id: str, version_id: str, ordinal: int, text: str,
               start: int, end: int) -> Chunk:
    return Chunk.new(doc_id=doc_id, version_id=version_id,
                     ordinal=ordinal, text=text, start=start, end=end)
