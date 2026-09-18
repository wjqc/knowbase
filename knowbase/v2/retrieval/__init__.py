"""V2 混合检索层（P2）。

子模块：
- tokenize        中英文统一分词（unicode 词 + 中文 bigram）
- channels        四路召回通道：exact / bm25 / dense / metadata
- rrf             Reciprocal Rank Fusion
- hybrid_search   顶层入口，组装四路 + RRF + governance_factor + reranker 桩
- shadow_adapter  与 V1 search_impl 产同形 output（用于对照评测）

设计原则：
- 各 channel 独立可替换（exact 与 metadata 在 P2 阶段做基础实现；后续接 hnswlib / 真 embedding）
- 不依赖 numpy / sklearn / torch（Pyodide 友好；评测可复现）
- dense embedding 用稳定 hash 桶替代真向量，便于无依赖跑对照；
  接入真模型时仅需替换 embeddings.encode()
"""
from .hybrid_search import (
    HybridSearch,
    HybridResult,
    SearchHit,
    build_hybrid_search_from_v1_repo,
)
from .shadow_adapter import format_as_v1

__all__ = [
    "HybridSearch",
    "HybridResult",
    "SearchHit",
    "build_hybrid_search_from_v1_repo",
    "format_as_v1",
]
