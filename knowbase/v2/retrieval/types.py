"""P2 retrieval 共享类型（避免循环依赖）。

将 SearchHit / HybridResult 抽到独立模块，便于 rerank / hybrid_search 互不依赖。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SearchHit:
    doc_id: str
    fused_score: float
    final_score: float
    per_channel: dict[str, float] = field(default_factory=dict)
    governance: dict | None = None
    rerank_score: float | None = None


@dataclass
class HybridResult:
    query: str
    hits: list[SearchHit]
    degraded: list[str] = field(default_factory=list)
    per_channel_top: dict[str, list[str]] = field(default_factory=dict)
    governance_enabled: bool = True
    reranker: str | None = None
    # --- P3-C ACL ---
    principal_id: str | None = None
    acl_enabled: bool = False
    acl_denied: int = 0
    acl_denied_ids: list[str] = field(default_factory=list)


__all__ = ["SearchHit", "HybridResult"]
