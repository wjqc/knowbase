"""Shadow adapter：把 V2 HybridSearch 包装成与 V1 search_impl 同形 output。"""
from __future__ import annotations

from .hybrid_search import HybridSearch, HybridResult


def format_as_v1(result: HybridResult, doc_index: dict[str, dict]) -> str:
    """与 V1 search_impl 输出同形：'命中 N 条' + 列表项；0 条 → '未命中「q」'。"""
    if not result.hits:
        return f"未命中「{result.query}」"
    lines = [f"命中 {len(result.hits)} 条"]
    for h in result.hits:
        meta = doc_index.get(h.doc_id, {})
        title = meta.get("title", "")
        kind = meta.get("kind", "")
        status = meta.get("status", "")
        per = " ".join(f"{k}={v:.2f}" for k, v in h.per_channel.items())
        suffix = f" ｜ {per}" if per else ""
        lines.append(f"- [{h.doc_id}] {title}（{kind}/{status}）{suffix}")
    return "\n".join(lines)
