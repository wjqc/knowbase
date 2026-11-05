"""八关系变更候选与修订保真（V2-06）。

核心不变式：
- 候选不覆盖已发布内容：propose 只落候选（含目标正文 spool），已发布
  head 不动；发布在 publication.py（V2-07）。
- claim 保真：ENRICH/CORRECT/REPLACE/MERGE/SPLIT 必须给出全部旧有效
  claim 的映射（preserved/modified/removed+moved），删除必须有理由引用；
  verified 卡缺一个映射就拒绝立案。
- 幂等：同 change_id + 同 proposal_hash 返回既有候选；同 id 异 hash 冲突。
- NO_CHANGE 单独处置：不产卡不产输出，只记材料处置与来源关联，绝不计为
  helpful。
- CONFLICT 不自动选边：只保留双方版本引用与冲突组，state 停在 needs_review。
"""
import hashlib
import json
from datetime import datetime, timezone

from . import contracts
from . import recall as _recall

STATES = ("proposed", "validating", "ready", "needs_review", "needs_rebase",
          "rejected", "expired", "published")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_change_id(relation: str) -> str:
    return "CH-" + hashlib.sha256(
        f"{relation}:{datetime.now(timezone.utc).isoformat()}"
        .encode("utf-8")).hexdigest()[:16]


def _proposal_hash(manifest: dict, outputs: list[dict]) -> str:
    return contracts.change_manifest_hash(
        {"manifest": manifest,
         "outputs": [{"memory_id": o.get("memory_id"), "snapshot_hash": o.get("snapshot_hash")}
                     for o in outputs]})


def _output_snapshot_hash(output: dict) -> str:
    body = output.get("body") or ""
    meta = output.get("meta") or {}
    return contracts.snapshot_hash(
        contracts.canonical_json({"meta": meta, "body": body}).encode("utf-8"))


def claim_coverage_errors(old_claims: list, claim_map: list) -> list[str]:
    """旧有效 claim 覆盖检查：每个旧 claim 必须出现在映射；removed 必须带
    justification_ref（已验证移除理由）；不允许无理由删除。"""
    problems = []
    mapped = {}
    for item in claim_map or []:
        if not isinstance(item, dict):
            problems.append("claim_map_item_not_object")
            continue
        mapped[item.get("old_claim_id")] = item
    for claim in old_claims or []:
        cid = claim.get("claim_id") if isinstance(claim, dict) else None
        if not cid:
            continue
        entry = mapped.get(cid)
        if entry is None:
            problems.append(f"old_claim_unmapped:{cid}")
        elif entry.get("mapping") == "removed" and not entry.get("justification_ref"):
            problems.append(f"removed_without_justification:{cid}")
    return problems


def propose_change(conn, repo, *, relation, risk, scope, base_heads, outputs,
                   source_dispositions, claim_map, proposer="",
                   source_material_refs=None, recall_snapshot=None,
                   old_claims=None, change_id=None,
                   policy_revision=contracts.CHANGE_POLICY_REVISION,
                   validation_plan_hash="") -> dict:
    """立案一个变更候选。不做模型调用、不改已发布内容。

    outputs 每项：{memory_id, disposition, meta, body}（NO_CHANGE 传 []）。
    base_heads 每项：{memory_id, revision_algo, content_revision}——与当前
    entity_heads 不符直接拒绝（REVISION_CONFLICT），不留带错 base 的候选。
    """
    if relation not in contracts.RELATIONS:
        return {"error": "relation_invalid", "relation": relation}
    # 冻结输出快照 hash。
    outputs = [dict(o) for o in (outputs or [])]
    for out in outputs:
        out["snapshot_hash"] = _output_snapshot_hash(out)
    if relation == "CONFLICT":
        outputs = []  # 冲突不产新内容，只留双方引用；需人工/验证裁决。
    manifest = {
        "change_id": change_id or _new_change_id(relation),
        "proposal_version": 1, "relation": relation, "risk": risk, "scope": scope,
        "source_material_refs": list(source_material_refs or []),
        "recall_snapshot_hash": (recall_snapshot or {}).get("heads_digest", ""),
        "base_heads": base_heads, "outputs": [
            {"memory_id": o["memory_id"], "snapshot_hash": o["snapshot_hash"],
             "disposition": o.get("disposition", "active")}
            for o in outputs],
        "source_dispositions": source_dispositions,
        "claim_map": claim_map,
        "policy_revision": policy_revision,
        "state": "needs_review" if relation == "CONFLICT" else "proposed",
        "validation_plan_hash": validation_plan_hash,
    }
    problems = contracts.validate_change_manifest(manifest)
    if problems:
        return {"error": "MANIFEST_INVALID", "problems": problems}
    # claim 保真：旧 claim（含无结构化 claim 卡的全卡视为单 claim C-whole）。
    effective_old = old_claims if old_claims is not None else (
        [{"claim_id": "C-whole"}] if relation in
        ("ENRICH", "CORRECT", "REPLACE", "MERGE", "SPLIT") and base_heads else [])
    coverage = claim_coverage_errors(effective_old, claim_map)
    if coverage:
        return {"error": "CLAIM_COVERAGE_INCOMPLETE", "problems": coverage}
    # base heads 必须与当前库一致（提案时就冻结输入版本）。
    for head in base_heads:
        row = conn.execute(
            "SELECT content_revision FROM entity_heads WHERE entity_id=?",
            (head["memory_id"],)).fetchone()
        if not row or row[0] != head["content_revision"]:
            return {"error": "REVISION_CONFLICT", "base_head": head["memory_id"],
                    "current": row[0] if row else None}
    # 召回快照漂移：材料对应语料已变化 → 重新召回判断。
    if recall_snapshot and _recall.snapshot_changed(conn, recall_snapshot):
        return {"error": "NEEDS_REASSESSMENT"}
    phash = _proposal_hash(manifest, outputs)
    change_id = manifest["change_id"]
    existing = conn.execute(
        "SELECT proposal_hash,state FROM lifecycle_changes WHERE change_id=?",
        (change_id,)).fetchone()
    if existing:
        if existing[0] != phash:
            return {"error": "IDEMPOTENCY_CONFLICT", "change_id": change_id}
        return {"change_id": change_id, "state": existing[1], "idempotent": True}
    now = _now()
    conn.execute(
        "INSERT INTO lifecycle_changes VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (change_id, relation, risk, scope, proposer,
         json.dumps(source_material_refs or [], ensure_ascii=False),
         manifest["recall_snapshot_hash"], json.dumps(base_heads, ensure_ascii=False),
         json.dumps({o["memory_id"]: {"disposition": o.get("disposition", "active"),
                                      "meta": o.get("meta"), "body_hash": o["snapshot_hash"]}
                     for o in outputs}, ensure_ascii=False),
         json.dumps(claim_map, ensure_ascii=False),
         json.dumps(source_dispositions, ensure_ascii=False),
         validation_plan_hash,
         hashlib.sha256(json.dumps([o.get("body", "") for o in outputs],
                                   ensure_ascii=False).encode("utf-8")).hexdigest(),
         phash, policy_revision,
         "needs_review" if relation == "CONFLICT" else "proposed", now, now))
    for out in outputs:
        conn.execute(
            "INSERT OR REPLACE INTO lifecycle_change_outputs VALUES(?,?,?,?,?,?)",
            (change_id, out["memory_id"], out.get("disposition", "active"),
             out["snapshot_hash"], json.dumps(out.get("meta") or {}, ensure_ascii=False),
             out.get("body") or ""))
    conn.commit()
    return {"change_id": change_id, "relation": relation, "risk": risk,
            "state": manifest["state"], "base_revision_frozen": True}


def get_change(conn, change_id: str) -> dict:
    row = conn.execute(
        "SELECT change_id,relation,risk,scope,proposer,base_heads_json,"
        "claim_map_json,source_dispositions_json,proposal_hash,policy_revision,"
        "state,created_at FROM lifecycle_changes WHERE change_id=?",
        (change_id,)).fetchone()
    if not row:
        return {"error": "NOT_FOUND"}
    outputs = [{"memory_id": r[0], "disposition": r[1], "snapshot_hash": r[2],
                "meta": json.loads(r[3]), "body": r[4]}
               for r in conn.execute(
                   "SELECT memory_id,disposition,snapshot_hash,meta_json,body "
                   "FROM lifecycle_change_outputs WHERE change_id=?", (change_id,))]
    return {"change_id": row[0], "relation": row[1], "risk": row[2], "scope": row[3],
            "proposer": row[4], "base_heads": json.loads(row[5]),
            "claim_map": json.loads(row[6]),
            "source_dispositions": json.loads(row[7]), "proposal_hash": row[8],
            "policy_revision": row[9], "state": row[10], "created_at": row[11],
            "outputs": outputs}


def list_changes(conn, state: str | None = None) -> list[dict]:
    sql = ("SELECT change_id,relation,risk,scope,state,created_at FROM "
           "lifecycle_changes")
    params = ()
    if state:
        sql += " WHERE state=?"
        params = (state,)
    return [{"change_id": r[0], "relation": r[1], "risk": r[2], "scope": r[3],
             "state": r[4], "created_at": r[5]}
            for r in conn.execute(sql + " ORDER BY created_at", params)]


def set_state(conn, change_id: str, state: str, *, reason: str = "") -> dict:
    if state not in STATES:
        return {"error": "state_invalid"}
    cur = conn.execute(
        "UPDATE lifecycle_changes SET state=?,updated_at=? WHERE change_id=?",
        (state, _now(), change_id))
    conn.commit()
    return {"change_id": change_id, "state": state, "updated": cur.rowcount == 1,
            "reason": reason}
