"""P3-B：ACL pre-filter（V2 计划 §7.2 — 在召回前硬过滤，channel 不可绕过）。

设计：
- pre_filter(principal, docs) -> (allowed, denied)；denied 仅供审计
- build_doc_predicate(principal) -> Callable[[dict], bool]；HybridSearch 接到 docs 之前按 predicate 过滤
- 失败关闭：policy 抛异常或返回 deny 的 doc 一律从 allowed 中剔除

不放进 retrieval/hybrid_search.py 是为了避免 V2 retrieval 子包依赖 ACL 子包形成
上层耦合；ACL 调用方（HybridSearch + Document read）单向依赖 ACL。
"""
from __future__ import annotations

from collections.abc import Callable, Iterable

from .policy import Policy, resolve
from .principal import Principal


def pre_filter(principal: Principal, docs: Iterable[dict]
               ) -> tuple[list[dict], list[tuple[dict, Policy]]]:
    """返回 (allowed_docs, denied_pairs)；denied_pairs 包含 (doc, policy) 供审计。"""
    allowed: list[dict] = []
    denied: list[tuple[dict, Policy]] = []
    for d in docs:
        policy = resolve(principal, d)
        if policy.allow:
            allowed.append(d)
        else:
            denied.append((d, policy))
    return allowed, denied


def build_doc_predicate(principal: Principal) -> Callable[[dict], bool]:
    """返回对单文档的布尔判定；HybridSearch 用 list(filter(pred, docs))。

    异常安全：resolve 永不抛异常（fail-closed 走 Policy(False)），predicate 不会漏过坏数据。
    """
    def _pred(doc: dict) -> bool:
        return resolve(principal, doc).allow
    return _pred