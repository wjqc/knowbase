"""宽召回：查重阶段的多路召回。

合并标题、关键词、code_refs、source_refs 等召回；
初始每路最多 50 条，经合并分页回传。
不照搬自动注入 verified 过滤，须覆盖同 scope 的 staging/once 候选。
"""

import json
import re
from pathlib import Path


def wide_recall(conn, query: str, scope: str, *,
                limit_per_route: int = 50,
                include_staging: bool = True) -> list[dict]:
    """多路宽召回，返回去重合并后的候选列表。

    召回路：
    1. FTS5 词法匹配
    2. 本地中文短词扩展（查重宽召回，不是语义向量）
    3. 标题关键词匹配
    4. code_refs 匹配
    """
    from .. import index

    query = (query or "").strip()
    if not query:
        # 空 query 会让 LIKE 路用 instr(..., '') 命中全部卡片，
        # 等于绕过 scope 过滤做全库召回，直接短路。
        return []

    results: dict[str, dict] = {}  # id -> best score info

    # 路 1: FTS5 词法
    terms = [t for t in (query or "").split() if len(t) >= 3]
    if terms:
        fts_ids = index._match_ids(conn, terms) or {}
        for mid, score in list(fts_ids.items())[:limit_per_route]:
            _merge(results, mid, score, "fts")

    # 路 2: bounded literal CJK phrases. Keep duplicate review broad after retiring hash vectors.
    allowed = _all_ids(conn, scope, include_staging)
    pieces = []
    for run in re.findall(r"[\u4e00-\u9fff]{2,}", query[:512]):
        pieces.extend(run[i:i+2] for i in range(len(run)-1))
    expanded = index._like_ids(conn, list(dict.fromkeys(pieces))[:48])
    for mid, score in sorted(expanded.items(), key=lambda x:-x[1]):
        if mid in allowed:
            _merge(results, mid, score * 0.5, "cjk_literal")

    # 路 3: LIKE 扫描（标题关键词）
    like_ids = index._like_ids(conn, terms or [query[:20]])
    for mid, score in list(like_ids.items())[:limit_per_route]:
        _merge(results, mid, score * 0.8, "title_like")

    # 路 4: code_refs 匹配（提取 code: 前缀）
    code_terms = re.findall(r'code:[\w./-]+', query)
    if code_terms:
        code_ids = index._like_ids(conn, code_terms)
        for mid, score in list(code_ids.items())[:limit_per_route]:
            _merge(results, mid, score * 0.9, "code_refs")

    # 组装结果，附加元数据
    output = []
    for mid, info in sorted(results.items(), key=lambda x: -x[1]["score"]):
        row = conn.execute(
            "SELECT id, title, scope, confidence, status, tags FROM meta WHERE id=?",
            (mid,),
        ).fetchone()
        if not row:
            continue
        # scope 过滤
        if scope and row[2] not in (scope, "global"):
            continue
        # staging 过滤
        if not include_staging:
            staging_row = conn.execute(
                "SELECT staging FROM meta WHERE id=?", (mid,)
            ).fetchone()
            if staging_row and staging_row[0]:
                continue
        output.append({
            "id": row[0],
            "title": row[1],
            "scope": row[2],
            "confidence": row[3],
            "status": row[4],
            "tags": json.loads(row[5] or "[]"),
            "score": round(info["score"], 6),
            "channels": info["channels"],
        })

    return output[:limit_per_route]


def _merge(results: dict, mid: str, score: float, channel: str):
    if mid in results:
        if score > results[mid]["score"]:
            results[mid]["score"] = score
        results[mid]["channels"].add(channel)
    else:
        results[mid] = {"score": score, "channels": {channel}}


def _all_ids(conn, scope: str, include_staging: bool) -> set[str]:
    conds = ["status != 'archived'"]
    params = []
    if scope:
        conds.append("(scope=? OR scope='global')")
        params.append(scope)
    if not include_staging:
        conds.append("staging = 0")
    rows = conn.execute(
        f"SELECT id FROM meta WHERE {' AND '.join(conds)}", params
    ).fetchall()
    return {r[0] for r in rows}
