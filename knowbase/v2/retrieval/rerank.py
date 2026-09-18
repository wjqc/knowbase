"""Cross-encoder reranker 占位（P2，J 阶段）。

设计要点：
- RerankerProtocol.rerank(query, hits) -> list[SearchHit]
- 接收 HybridSearch 输出的候选（已带 fused_score / governance），
  重新打分并按新 final_score 排序
- 不修改 fused_score，只在 final_score 之上叠加 rerank_signal
- 默认 TokenOverlapReranker：query 与 (title + body) 的 token F1，重在"召回有但 F1 高"
- 真 cross-encoder 接入时仅需新增一个实现，不影响 HybridSearch / channels

分数合成公式：
    rerank_score ∈ [0, 1]
    final_score = w_rerank × rerank_score + (1 - w_rerank) × normalised_fused

其中 normalised_fused = min(1, fused_score)（fused 通常 << 1）
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Protocol, runtime_checkable

from .tokenize import tokenize_set
from .types import SearchHit


@runtime_checkable
class RerankerProtocol(Protocol):
    """Cross-encoder reranker 协议。

    - name: 用于 HybridResult 记录与 audit
    - model_version: 用于双索引切换（同 governance）
    - rerank(query, hits): 必须确定性；返回新列表（不修改入参）
    """

    name: str
    model_version: str

    def rerank(self, query: str, hits: list[SearchHit],
               *, doc_index: dict[str, dict]) -> list[SearchHit]: ...


def _token_f1(q_tokens: set[str], doc_tokens: set[str]) -> float:
    """token 集合 F1 ∈ [0, 1]。空集返回 0。"""
    if not q_tokens or not doc_tokens:
        return 0.0
    overlap = q_tokens & doc_tokens
    if not overlap:
        return 0.0
    p = len(overlap) / len(q_tokens)
    r = len(overlap) / len(doc_tokens)
    return 2 * p * r / (p + r)


@dataclass
class TokenOverlapReranker:
    """基于 query 与 doc 文本 token F1 的 reranker 占位。

    文本字段：title 权重 2，body 权重 1（rerank 内置，不影响通道）。
    """

    name: str = "token_overlap"
    model_version: str = "token-f1-v1"
    title_weight: float = 2.0
    body_weight: float = 1.0

    def _score(self, query: str, doc: dict) -> float:
        q_set = tokenize_set(query)
        if not q_set:
            return 0.0
        title = (doc.get("title") or "")
        body = (doc.get("body") or "")
        title_set = tokenize_set(title)
        body_set = tokenize_set(body)
        f1_t = _token_f1(q_set, title_set) if title_set else 0.0
        f1_b = _token_f1(q_set, body_set) if body_set else 0.0
        # 加权平均，权重 0 时该项忽略
        w_t = self.title_weight if title_set else 0.0
        w_b = self.body_weight if body_set else 0.0
        denom = w_t + w_b
        if denom == 0:
            return 0.0
        return (w_t * f1_t + w_b * f1_b) / denom

    def rerank(self, query: str, hits: list[SearchHit],
               *, doc_index: dict[str, dict],
               w_rerank: float = 0.7) -> list[SearchHit]:
        """对 hits 重新打分并按 final_score 降序返回。

        final_score = w_rerank * rerank_score + (1 - w_rerank) * min(1, fused_score)
        若 fused_score 为 0（即文档被 governance 整除），保留原 final_score。
        """
        out: list[SearchHit] = []
        for h in hits:
            doc = doc_index.get(h.doc_id, {})
            rs = self._score(query, doc)
            fused_norm = min(1.0, max(0.0, h.fused_score))
            if h.final_score == 0.0 and h.fused_score > 0.0:
                # 被 governance 整除 → 不引入 rerank 信号，避免拉回
                new_final = 0.0
            else:
                new_final = w_rerank * rs + (1 - w_rerank) * h.final_score
            out.append(SearchHit(
                doc_id=h.doc_id,
                fused_score=h.fused_score,
                final_score=new_final,
                per_channel=h.per_channel,
                governance=h.governance,
            ))
        out.sort(key=lambda x: x.final_score, reverse=True)
        return out


# ---------- 工厂 ----------

def default_reranker() -> RerankerProtocol:
    """返回默认 reranker（占位）。升级时换实现即可。"""
    return TokenOverlapReranker()


__all__ = [
    "RerankerProtocol",
    "TokenOverlapReranker",
    "default_reranker",
]
