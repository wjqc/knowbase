"""多机物化与一致性（V2-14）。

核心机制已分布在各模块并在测试中演练：
- 证据物化与去重/撤销：evidence.import_package（V2-13）；
- 治理重算：governance.decide/apply_decision——接收机必须本地重算，
  同步来的 decision 只是可核验声明；
- 发布权威与恢复：publication（manifest 是 durable commit point）。

本模块补两块残余：
- detect_divergent_heads：按 publication manifest parents 检测并发版本头
  （两机各发 r2a/r2b）——Git 文本可合并不等于语义可合并，检测到即隔离
  待显式 MERGE/CORRECT 候选，不 last-writer 获胜。
- materialize_imported + recompute：导入证据后的本地治理重算入口
  （幂等：同证据集合 digest 同输出）。
"""
import json
from pathlib import Path

from . import governance, revisions


def detect_divergent_heads(repo: Path, conn) -> list[dict]:
    """扫描发布 manifest DAG，返回并发头组（同 base 不同 after 的卡）。

    冲突处理不在本函数：保留双方版本与证据，生成待裁决清单（设计 §10.4）。"""
    repo = Path(repo)
    divergent = []
    rows = conn.execute(
        "SELECT publication_id,change_id,manifest_path,state FROM publication_batches "
        "WHERE state IN ('published','materialized')").fetchall()
    by_memory: dict[str, list[dict]] = {}
    for publication_id, change_id, manifest_path, state in rows:
        path = repo / manifest_path
        if not path.is_file():
            continue
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        ba = {}
        try:
            ba = json.loads(conn.execute(
                "SELECT before_after_hashes_json FROM publication_batches WHERE "
                "publication_id=?", (publication_id,)).fetchone()[0] or "{}")
        except (TypeError, ValueError):
            pass
        for memory_id, revs in ba.items():
            by_memory.setdefault(memory_id, []).append(
                {"publication_id": publication_id, "change_id": change_id,
                 "before": revs.get("before", ""), "after": revs.get("after", "")})
    for memory_id, pubs in by_memory.items():
        # 同一 before 派生出多个不同 after → 并发头。
        afters = {}
        for pub in pubs:
            if pub["before"]:
                afters.setdefault(pub["before"], set()).add(pub["after"])
        for before, after_set in afters.items():
            if len(after_set) > 1:
                divergent.append({"memory_id": memory_id, "base": before,
                                  "heads": sorted(after_set),
                                  "publications": [p["publication_id"] for p in pubs
                                                   if p["before"] == before]})
    return divergent


def sync_state(conn) -> dict:
    """本机证据/发布水位（离线可服务最后本地确认版本，显示 as_of）。"""
    evidence_watermark = conn.execute(
        "SELECT MAX(occurred_at) FROM evidence_events WHERE import_state IN "
        "('local','imported')").fetchone()[0] or ""
    revocations = conn.execute(
        "SELECT COUNT(*) FROM evidence_events WHERE event_type='evidence.revoked'"
    ).fetchone()[0]
    publications = conn.execute(
        "SELECT state,COUNT(*) FROM publication_batches GROUP BY state").fetchall()
    return {"evidence_watermark": evidence_watermark,
            "revocations_total": revocations,
            "publications": dict(publications),
            "note": "local watermark; remote convergence requires exchange + recompute"}


def materialize_and_recompute(repo, conn, *, cfg=None) -> dict:
    """导入证据后的本地治理重算（幂等入口；V2-14 流程第 4 步）。"""
    decisions = governance.recompute_all(repo, conn, cfg=cfg)
    return {"recomputed": len(decisions),
            "decisions": [{"memory_id": d.get("memory_id"),
                           "decision": d.get("decision"),
                           "digest": d.get("input_set_digest", "")[:16]}
                          for d in decisions if not d.get("error")],
            "divergent_heads": detect_divergent_heads(repo, conn)}
