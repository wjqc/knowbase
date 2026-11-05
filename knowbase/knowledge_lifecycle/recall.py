"""演进检索（V2-05）：为内容关系判断召回候选，而非任务建议。

与任务检索的差别（设计 §11）：
- 域：同授权 scope 内 staging/stale 必须参与（补旧卡/纠正旧卡都要找到它们）；
  archived 只在明确治理目的（复活/替代检索）时参与。
- 排序：候选覆盖与冲突召回优先，不因旧卡 stale/staging 隐藏，不套用任务
  检索的 active-only 过滤。
- 冻结：recall_snapshot 绑定语料 heads（候选 + 版本 + 通道 + 截断诊断），
  提交候选时语料变化可检出（NEEDS_REASSESSMENT 语义的基础）。

无 Embedding 时保留多路词法；语义演进代际（purpose=evolution）在模型
POC 通过前不参与（返回 lexical_only 降级标记）。
"""
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from ..extraction import recall as _wide
from . import contracts

ROUTE_LIMIT = 50          # 每路召回上限（任务书 V2-05 §6）
FUSED_LIMIT = 30          # 融合去重后上限
_ERROR_CODE_RE = re.compile(r"\b(?:HTTP\s*)?\d{3}\b|\bECONN\w+|\bERR_\w+|\b0x[0-9A-Fa-f]+\b")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _heads_digest(conn, ids: list[str]) -> str:
    rows = conn.execute(
        "SELECT entity_id,content_revision FROM entity_heads WHERE entity_id IN "
        "(%s) ORDER BY entity_id" % ",".join("?" * len(ids)), ids).fetchall() if ids else []
    return contracts.snapshot_hash(
        contracts.canonical_json({"heads": rows}).encode("utf-8"))[:64]


def _domain_heads_digest(conn, scope: str) -> str:
    """语料域 heads：快照漂移检测绑定整个授权域（含 staging/stale），
    新卡出现也会被检出（NEEDS_REASSESSMENT 语义），不只盯已召回候选。"""
    rows = conn.execute(
        "SELECT m.id,h.content_revision FROM meta m JOIN entity_heads h ON "
        "h.entity_id=m.id WHERE (m.scope=? OR m.scope='global') AND m.status!='archived' "
        "ORDER BY m.id", (scope,)).fetchall()
    return contracts.snapshot_hash(
        contracts.canonical_json({"domain": scope, "heads": rows}).encode("utf-8"))[:64]


def evolution_recall(conn, query: str, scope: str, *,
                     include_archived: bool = False,
                     limit: int = FUSED_LIMIT,
                     corpus_version: str = "v1-index") -> dict:
    """演进域宽召回。只做本 scope + global，不做跨项目全库查重。

    返回 {"snapshot": {...}, "candidates": [...], "degraded": [...]}：
    - candidates 含 id/content_revision/status/confidence/staging/scope/
      channels/rank（含 staging/stale，含错误码精确命中标记）。
    - snapshot 为可冻结的召回快照（purpose/heads digest/截断诊断）。
    - degraded 列出降级原因（当前恒含 lexical_only：无演进语义代际）。
    """
    query = (query or "").strip()
    if not query or not scope:
        return {"error": "query_and_scope_required"}
    degraded = ["lexical_only"]
    # wide_recall 域已含 staging；stale 卡在 meta.status 中，wide_recall
    # 只排 archived，天然满足"staging/stale 参与"。archived 由本函数显式追加。
    base = _wide.wide_recall(conn, query, scope, limit_per_route=ROUTE_LIMIT)
    candidates: dict[str, dict] = {}
    for hit in base:
        candidates[hit["id"]] = {**hit, "rank": 0}
    # 错误码/对象精确路：宽召回按词法融合可能压掉低频精确串，单独补一路。
    codes = _ERROR_CODE_RE.findall(query[:512])
    if codes:
        from .. import index
        exact = index._like_ids(conn, [f" {c} " for c in codes][:12])
        allowed = _allowed_domain(conn, scope, include_archived)
        for mid, score in sorted(exact.items(), key=lambda x: -x[1])[:ROUTE_LIMIT]:
            if mid not in allowed:
                continue
            row = conn.execute(
                "SELECT id,title,scope,confidence,status,tags,staging FROM meta WHERE id=?",
                (mid,)).fetchone()
            if not row:
                continue
            entry = candidates.setdefault(mid, {
                "id": row[0], "title": row[1], "scope": row[2], "confidence": row[3],
                "status": row[4], "tags": json.loads(row[5] or "[]"), "score": 0.0,
                "channels": set(), "staging": bool(row[6])})
            entry["channels"].add("error_code_exact")
            entry["score"] = max(entry.get("score", 0.0), score * 0.95)
    # 关系邻接：top 命中卡 frontmatter 的 relations 指向的卡补入候选
    # （同 scope 域内），保证替代/矛盾对象不被词法漏掉。
    adjacency_added = 0
    for mid in list(candidates)[:10]:
        row = conn.execute("SELECT path FROM meta WHERE id=?", (mid,)).fetchone()
        if not row or not row[0]:
            continue
        try:
            from .. import store
            meta, _body = store.parse(Path(row[0]))
        except (OSError, ValueError):
            continue
        for rel in meta.get("relations") or []:
            rid = rel.get("id") if isinstance(rel, dict) else None
            if not rid or rid in candidates:
                continue
            info = conn.execute(
                "SELECT id,title,scope,confidence,status,tags,staging FROM meta WHERE id=?",
                (rid,)).fetchone()
            if not info or info[2] not in (scope, "global"):
                continue
            if info[4] == "archived" and not include_archived:
                continue
            candidates[rid] = {
                "id": info[0], "title": info[1], "scope": info[2], "confidence": info[3],
                "status": info[4], "tags": json.loads(info[5] or "[]"),
                "score": 0.25, "channels": {"relation_adjacency"}, "staging": bool(info[6])}
            adjacency_added += 1
    # 排序 + 版本绑定 + 截断诊断
    ordered = sorted(candidates.values(), key=lambda c: (-c.get("score", 0), c["id"]))
    truncated = max(0, len(ordered) - limit)
    final = []
    for rank, entry in enumerate(ordered[:limit], 1):
        head = conn.execute(
            "SELECT content_revision FROM entity_heads WHERE entity_id=?",
            (entry["id"],)).fetchone()
        final.append({
            "id": entry["id"], "title": entry.get("title", ""),
            "content_revision": head[0] if head else "",
            "revision_algo": "content-v1", "scope": entry.get("scope", ""),
            "status": entry.get("status", ""), "confidence": entry.get("confidence", ""),
            "staging": bool(entry.get("staging", False)),
            "channels": sorted(entry.get("channels", [])),
            "score": round(float(entry.get("score", 0.0)), 6), "rank": rank})
    ids = [c["id"] for c in final]
    snapshot = {
        "schema": "knowbase.evolution-recall.v1",
        "purpose": "evolution",
        "generated_at": _now(),
        "scope": scope,
        "query_hash": contracts.snapshot_hash(query.encode("utf-8")),
        "corpus_version": corpus_version,
        "heads_digest": _heads_digest(conn, ids),
        "domain_heads_digest": _domain_heads_digest(conn, scope),
        "candidate_ids": ids,
        "route_limit": ROUTE_LIMIT, "fused_limit": limit,
        "truncated": truncated, "adjacency_added": adjacency_added,
        "degraded": degraded,
        "note": "staging/stale included; archived excluded unless governance purpose",
    }
    return {"snapshot": snapshot, "candidates": final, "degraded": degraded}


def _allowed_domain(conn, scope: str, include_archived: bool) -> set[str]:
    conds = ["(scope=? OR scope='global')", "staging IN (0,1)"]
    params = [scope]
    if not include_archived:
        conds.append("status != 'archived'")
    return {r[0] for r in conn.execute(
        f"SELECT id FROM meta WHERE {' AND '.join(conds)}", params)}


def snapshot_changed(conn, snapshot: dict) -> bool:
    """提交候选前核对召回快照是否仍与语料一致。

    双 digest：候选 heads 变化（候选卡被改）或域 heads 变化（同 scope 出现
    新相关卡）都判漂移，提交侧必须重新召回与判断。"""
    ids = snapshot.get("candidate_ids") or []
    if _heads_digest(conn, ids) != snapshot.get("heads_digest"):
        return True
    scope = snapshot.get("scope") or ""
    if snapshot.get("domain_heads_digest"):
        return _domain_heads_digest(conn, scope) != snapshot["domain_heads_digest"]
    return False
