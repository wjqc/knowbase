"""四路召回通道（P2）。

统一接口：`Channel.rank(query, top_k) -> list[(doc_id, score)]`
- ExactChannel: ID / 标题 / 路径 / 错误码精确命中
- BM25Channel:  BM25(FTS5 等价) 文本相关
- DenseChannel: cosine(emb(q), emb(doc))；encoder 可注入（HashEncoder / TfidfEncoder / 真模型）
- MetadataChannel: 同 kind / 同 tag / 路径相似度（Phase 3 接 ACL/project）
"""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import Iterable

from .bm25 import BM25
from .embeddings import EncoderProtocol, default_encoder
from .tokenize import tokenize_set


class Channel(ABC):
    name: str = "abstract"

    @abstractmethod
    def rank(self, query: str, top_k: int) -> list[tuple[str, float]]: ...


class ExactChannel(Channel):
    """ID/标题/正文精确子串匹配。无依赖。"""
    name = "exact"

    def __init__(self, docs: list[dict]):
        # docs: [{doc_id, title, body, path?}]
        self.docs = docs
        self._needle_cache: list[tuple[str, str, str, str]] = [
            (d["doc_id"], d.get("title", ""), d.get("body", ""), d.get("path", ""))
            for d in docs
        ]

    def rank(self, query: str, top_k: int) -> list[tuple[str, float]]:
        q = (query or "").strip()
        if not q:
            return []
        out: list[tuple[str, float, int]] = []  # (id, score, priority)
        for doc_id, title, body, path in self._needle_cache:
            score = 0.0
            if q == doc_id:
                score += 100.0
            if title and q in title:
                score += 30.0 + max(0, 10 - len(title))
            if path and q in path:
                score += 20.0
            if body and q in body:
                # 多次出现累加
                score += min(5.0, float(body.count(q)))
            if score > 0:
                out.append((doc_id, score, 0))
        out.sort(key=lambda x: x[1], reverse=True)
        return [(i, s) for i, s, _ in out[:top_k]]


class BM25Channel(Channel):
    name = "bm25"

    def __init__(self, docs: list[dict]):
        self.docs = docs
        self.bm25 = BM25()
        # 文本拼接：title + body；title 加权靠重复加入
        corpus: list[str] = []
        for d in docs:
            title = d.get("title", "")
            body = d.get("body", "")
            text = (title + " " if title else "") + body
            corpus.append(text)
        self.bm25.add_documents(corpus)

    def rank(self, query: str, top_k: int) -> list[tuple[str, float]]:
        results = self.bm25.rank(query, top_k)
        return [(self.docs[i]["doc_id"], s) for i, s in results]


class DenseChannel(Channel):
    name = "dense"

    def __init__(
        self,
        docs: list[dict],
        encoder: EncoderProtocol | None = None,
    ):
        self.docs = docs
        self.encoder: EncoderProtocol = encoder or default_encoder()
        self.model_version: str = self.encoder.model_version
        self.dim: int = self.encoder.dim
        self.vectors: list[list[float]] = []
        for d in docs:
            text = (d.get("title", "") + " " + d.get("body", "")).strip()
            self.vectors.append(self.encoder.encode(text))

    def rank(self, query: str, top_k: int) -> list[tuple[str, float]]:
        if not query.strip():
            return []
        qv = self.encoder.encode(query)
        if all(v == 0.0 for v in qv):
            return []
        scored = []
        for i, dv in enumerate(self.vectors):
            from .embeddings import cosine
            s = cosine(qv, dv)
            if s > 0:
                scored.append((self.docs[i]["doc_id"], s))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]


class MetadataChannel(Channel):
    """基于 kind / tag / 路径元数据的轻量匹配；Phase 3 替换为 ACL/project filter。"""
    name = "metadata"

    def __init__(self, docs: list[dict]):
        self.docs = docs

    def rank(self, query: str, top_k: int) -> list[tuple[str, float]]:
        q_tokens = tokenize_set(query)
        if not q_tokens:
            return []
        out: list[tuple[str, float]] = []
        for d in self.docs:
            score = 0.0
            tag_set = set(d.get("tags", []) or [])
            kind = d.get("kind", "")
            overlap = len(q_tokens & tag_set)
            if overlap:
                score += 2.0 * overlap
            # 同 kind 弱加权
            if kind and kind in q_tokens:
                score += 1.0
            # 路径重合
            path = d.get("path", "")
            if path:
                path_tokens = set(path.replace("/", " ").replace(".", " ").split())
                if q_tokens & path_tokens:
                    score += 1.5
            if score > 0:
                out.append((d["doc_id"], score))
        out.sort(key=lambda x: x[1], reverse=True)
        return out[:top_k]
