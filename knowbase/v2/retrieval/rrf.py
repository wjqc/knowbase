"""Reciprocal Rank Fusion（P2）。"""
from __future__ import annotations


def rrf(rankings: dict[str, list[tuple[str, float]]], k: int = 60) -> list[tuple[str, float]]:
    """输入：channel_name -> [(doc_id, score)]（已按 score desc 排序）
    输出：[(doc_id, fused_score)] 按 fused_score desc 排序
    """
    fused: dict[str, float] = {}
    for channel, lst in rankings.items():
        for rank, (doc_id, _score) in enumerate(lst, start=1):
            fused[doc_id] = fused.get(doc_id, 0.0) + 1.0 / (k + rank)
    return sorted(fused.items(), key=lambda x: x[1], reverse=True)
