"""BM25 检索通道（P2，G 阶段切换到 rank_bm25 标准实现）。

用 rank_bm25.BM25Okapi 作为底层，沿用 V2 tokenize() 做分词，
保留 P2 公共 API（BM25 / add_documents / score_one / rank），
保证 BM25Channel 无需改动。

参数：k1=1.5, b=0.75（rank_bm25 默认）
"""
from __future__ import annotations

from typing import Iterable, Sequence

from rank_bm25 import BM25Okapi

from .tokenize import tokenize


class BM25:
    """BM25 检索器（包装 rank_bm25.BM25Okapi）。

    设计要点：
    - corpus 增量加入：每次 add_documents 重建索引（小语料场景）。
    - 不暴露 numpy 数组，只暴露纯 Python 接口。
    - score_one(idx, query) 走底层 BM25Okapi.get_scores，兼容旧 API。
    """

    def __init__(self, k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self._texts: list[str] = []
        self._index: BM25Okapi | None = None

    # ---------- 索引构建 ----------
    def add_documents(self, texts: Iterable[str]) -> None:
        """增量加入文档；每次重建索引（适合 ≤ 10k 小语料）。"""
        new_texts = list(texts)
        if not new_texts:
            return
        self._texts.extend(new_texts)
        tokenized_corpus: list[list[str]] = [tokenize(t) for t in self._texts]
        self._index = BM25Okapi(tokenized_corpus, k1=self.k1, b=self.b)

    def __len__(self) -> int:
        return len(self._texts)

    # ---------- 检索 ----------
    def score_one(self, query: str, idx: int) -> float:
        """返回 query 对第 idx 个文档的 BM25 分数。"""
        if self._index is None or idx < 0 or idx >= len(self._texts):
            return 0.0
        q_tokens = tokenize(query)
        if not q_tokens:
            return 0.0
        scores = self._index.get_scores(q_tokens)
        return float(scores[idx])

    def rank(self, query: str, top_k: int = 50) -> list[tuple[int, float]]:
        """返回按 BM25 分数降序的 [(doc_idx, score), ...]。"""
        if self._index is None:
            return []
        q_tokens = tokenize(query)
        if not q_tokens:
            return []
        scores = self._index.get_scores(q_tokens)
        # 仅保留正分；空 query / 全 0 分数下返回空列表
        scored: list[tuple[int, float]] = [
            (int(i), float(s)) for i, s in enumerate(scores) if s > 0
        ]
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]


__all__ = ["BM25"]
