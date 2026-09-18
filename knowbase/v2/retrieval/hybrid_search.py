"""V2 混合检索主入口（P2 + P3-C ACL 前置过滤）。

流程：
  召回四路 → RRF 融合 → governance_factor 后置 → reranker 重排 → Top K

P3-C：ACL 在召回前硬过滤（§7.2 fail-closed）。
  - HybridSearch 接受 principal / acl_predicate；任一非 None 即视为启用
  - 已过滤 docs 用于构造 channels + doc_index，channel 拿不到无权 doc
  - HybridResult 记录 acl_denied / acl_denied_ids / principal_id
  - principal=None 且 acl_predicate=None 时行为完全不变（V2 计划 §6 向后兼容）
"""
from __future__ import annotations

from typing import Callable

from ..acl import Principal, build_doc_predicate, pre_filter
from .channels import (
    BM25Channel,
    Channel,
    DenseChannel,
    ExactChannel,
    MetadataChannel,
)
from .governance import compute_governance_factor
from .rerank import RerankerProtocol
from .rrf import rrf
from .types import HybridResult, SearchHit

_ACLPredicate = Callable[[dict], bool]


class HybridSearch:
    def __init__(self, docs: list[dict], *, k: int = 60,
                 channels: list[Channel] | None = None,
                 top_k_per_channel: int = 50,
                 apply_governance: bool = True,
                 doc_index: dict[str, dict] | None = None,
                 reranker: RerankerProtocol | None = None,
                 rerank_pool_size: int = 30,
                 principal: Principal | None = None,
                 acl_predicate: _ACLPredicate | None = None):
        """docs: 全部文档；doc_index: doc_id → doc 元数据（用于 governance）。

        如果未传 doc_index，会用 {d["doc_id"]: d for d in docs} 自动构建。

        rerank_pool_size: rerank 前的候选池大小（V2 计划 §6.3 建议 30）。

        P3-C ACL：
          principal: 运行时身份；提供后会在 __init__ 立即按 build_doc_predicate
                     过滤 docs，channel 完全拿不到无权 doc（fail-closed）。
          acl_predicate: 自定义谓词；与 principal 二选一，acl_predicate 优先。
                         当两者都为 None 时不过滤（向后兼容 V1/P2）。
        """
        # ---- P3-C ACL：__init__ 立即过滤，channel 不可绕过 ----
        if acl_predicate is not None:
            self._acl_enabled = True
            self._principal: Principal | None = None
            self._acl_pred = acl_predicate
            allowed: list[dict] = [d for d in docs if acl_predicate(d)]
            denied_ids: list[str] = [
                d.get("doc_id") for d in docs
                if not acl_predicate(d) and d.get("doc_id") is not None
            ]
        elif principal is not None:
            self._acl_enabled = True
            self._principal = principal
            self._acl_pred = build_doc_predicate(principal)
            allowed, denied = pre_filter(principal, docs)
            denied_ids = [d.get("doc_id") for d, _ in denied if d.get("doc_id") is not None]
        else:
            self._acl_enabled = False
            self._principal = None
            self._acl_pred = None
            allowed = docs
            denied_ids = []

        self.docs = allowed  # 仅保留 allowed；self.docs 暴露给外部用于审计
        self.acl_denied_ids: list[str] = denied_ids

        self.k = k
        self.top_k_per_channel = top_k_per_channel
        self.apply_governance = apply_governance
        self.doc_index: dict[str, dict] = (
            doc_index if doc_index is not None
            else {d["doc_id"]: d for d in self.docs if "doc_id" in d}
        )
        self.reranker = reranker
        self.rerank_pool_size = rerank_pool_size

        if channels is None:
            # 默认 channels 全部用 allowed docs，channel 拿不到无权 doc
            self.channels: list[Channel] = [
                ExactChannel(self.docs),
                BM25Channel(self.docs),
                DenseChannel(self.docs),
                MetadataChannel(self.docs),
            ]
        else:
            # 用户自定义 channels：必须已用 allowed docs 构造。
            # 调用方应通过 HybridSearch 暴露的 self.docs 重建 channel，
            # 否则 channel 可能引用未被过滤的 doc（fail-closed 由调用方负责）。
            self.channels = channels

    def search(self, query: str, top_k: int = 5) -> HybridResult:
        rankings: dict[str, list[tuple[str, float]]] = {}
        per_channel_top: dict[str, list[str]] = {}
        for ch in self.channels:
            try:
                ranked = ch.rank(query, self.top_k_per_channel)
            except Exception:
                ranked = []
            rankings[ch.name] = ranked
            per_channel_top[ch.name] = [d for d, _ in ranked[:top_k]]

        fused = rrf(rankings, k=self.k)

        # 候选池：rerank 前允许覆盖更多，governance 不会引入新的 hit
        pool_size = max(
            self.rerank_pool_size if self.reranker else top_k,
            top_k,
        )
        scored: list[SearchHit] = []
        for doc_id, fscore in fused[:pool_size]:
            per: dict[str, float] = {}
            for ch_name, lst in rankings.items():
                for r, (i, s) in enumerate(lst, start=1):
                    if i == doc_id:
                        per[ch_name] = s
                        break
            gov = None
            final = fscore
            if self.apply_governance:
                doc = self.doc_index.get(doc_id, {})
                br = compute_governance_factor(doc)
                gov = br.as_dict()
                final = fscore * br.factor
            scored.append(SearchHit(
                doc_id=doc_id,
                fused_score=fscore,
                final_score=final,
                per_channel=per,
                governance=gov,
            ))

        # Rerank（如配置）
        if self.reranker is not None and scored:
            scored = self.reranker.rerank(query, scored, doc_index=self.doc_index)

        # 按 final_score 降序，取 top_k
        scored.sort(key=lambda x: x.final_score, reverse=True)
        scored = scored[:top_k]

        return HybridResult(
            query=query,
            hits=scored,
            per_channel_top=per_channel_top,
            governance_enabled=self.apply_governance,
            reranker=self.reranker.name if self.reranker else None,
            principal_id=self._principal.principal_id if self._principal is not None else None,
            acl_enabled=self._acl_enabled,
            acl_denied=len(self.acl_denied_ids),
            acl_denied_ids=list(self.acl_denied_ids),
        )


def build_hybrid_search_from_v1_repo(docs: list[dict]) -> HybridSearch:
    """便于对照评测：直接灌入已加载的 docs（不过 ACL）。"""
    return HybridSearch(docs=docs)