"""变更验证引擎（V2-08）：确定性检查为主，验证结果 pass/fail/unknown。

设计 §6.1：生成候选的同一个 LLM 不得同时编写唯一预期并宣告通过——本引擎
全部为确定性核对（版本/出处/claim 映射/证据绑定/差异冻结），模型输出只
进候选；R2/R3 的独立复核与权威确认在 admission.py。
"""
import hashlib
import json
from datetime import datetime, timezone

from .. import store
from . import changes as _changes
from . import admission, contracts, governance

VALIDATOR_ID = "lifecycle-validation-v1"

# 统一生命周期（2026-11-09）：类型不再参与验证门槛。权威门只看共享范围
# （risk=R3）；出处不足由准入层升风险进待审，不在此按类型硬拒。


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _card_claims(meta: dict) -> list[dict]:
    claims = meta.get("claims")
    if isinstance(claims, list) and claims:
        return [c for c in claims if isinstance(c, dict) and c.get("claim_id")]
    return [{"claim_id": "C-whole", "subject": meta.get("title", "")}]


def validate_change(conn, repo, change_id: str) -> dict:
    """对一个变更候选执行验证计划，返回并落盘验证报告。

    检查集（确定性）：
    - base_current：base heads 与当前 entity_heads 一致（不一致 → fail，
      候选需 needs_rebase）。
    - claim_coverage：全部旧有效 claim 有映射；removed 有理由引用。
    - old_claim_evidence：preserved claim 的旧证据绑定旧 revision 存在
      （无任何证据 → unknown，不判 pass）。
    - output_frozen：输出 spool 内容与 snapshot_hash 一致、内容关系有输出。
    - risk_authority：R3 及 strict 类型修改需要 authority_ref（无 → fail，
      AUTHORITY_REQUIRED 语义在报告中单列）。
    """
    change = _changes.get_change(conn, change_id)
    if change.get("error"):
        return change
    checks = []
    overall = "pass"

    def record(check, result, detail="", gating=True):
        nonlocal overall
        checks.append({"check": check, "result": result, "detail": detail})
        if not gating:
            return
        if result == "fail" or (result == "unknown" and overall == "pass"):
            overall = result

    # base_current
    for head in change["base_heads"]:
        row = conn.execute(
            "SELECT content_revision FROM entity_heads WHERE entity_id=?",
            (head["memory_id"],)).fetchone()
        if not row or row[0] != head["content_revision"]:
            record("base_current", "fail",
                   f"{head['memory_id']} head moved to {row[0] if row else None}")
            break
    else:
        record("base_current", "pass")

    # claim_coverage + old evidence
    for head in change["base_heads"]:
        meta, body, path = store.load(repo, head["memory_id"])
        if not meta:
            record("claim_coverage", "fail", f"{head['memory_id']} card missing")
            continue
        old_claims = _card_claims(meta)
        problems = _changes.claim_coverage_errors(old_claims, change["claim_map"])
        if problems:
            record("claim_coverage", "fail", ";".join(problems))
        else:
            record("claim_coverage", "pass", f"{len(old_claims)} old claims mapped")
        evidence_rows = sum(
            event["polarity"] == "positive" and event["trust_basis"] == "registered-adapter"
            for event in governance.effective_evidence(conn, head["memory_id"], head["content_revision"]))
        # R0/R1：无旧样本记 unknown 不阻断——可准入为 once，不自动 verified。
        # R2/R3（修改 verified/多卡/危险动作）：缺回归样本即 fail。
        if evidence_rows:
            record("old_claim_evidence", "pass",
                   f"evidence_rows={evidence_rows} on base revisions")
        elif change["risk"] in ("R2", "R3"):
            record("old_claim_evidence", "fail",
                   "regression samples missing for high-risk change")
        else:
            record("old_claim_evidence", "unknown",
                   "no bound evidence on base revision; publishable as once only",
                   gating=False)

    # output_frozen
    outputs = change["outputs"]
    if change["relation"] in ("NO_CHANGE", "CONFLICT"):
        record("output_frozen", "pass" if not outputs else "fail",
               "content-less relations must not carry outputs")
    elif not outputs:
        record("output_frozen", "fail", "content relation without outputs")
    else:
        frozen_ok = True
        for out in outputs:
            recomputed = contracts.snapshot_hash(
                contracts.canonical_json({"meta": out["meta"], "body": out["body"]})
                .encode("utf-8"))
            if recomputed != out["snapshot_hash"]:
                frozen_ok = False
        record("output_frozen", "pass" if frozen_ok else "fail",
               f"{len(outputs)} outputs hash-verified")
    # A confirmation must cover the exact output revision, not the old head or a different card.
    from ..mutations import _compute_revision
    authority_refs = []
    for out in outputs:
        if out["disposition"] != "active":
            continue
        row = conn.execute(
            "SELECT authority_ref FROM admission_reports WHERE change_id=? AND "
            "memory_id=? AND content_revision=? AND validator_id=? "
            "AND result='pass' AND authority_ref!='' ORDER BY created_at DESC LIMIT 1",
            (change_id, out["memory_id"], _compute_revision(out["meta"], out["body"]),
             admission.ADMISSION_VALIDATOR_ID)).fetchone()
        if row:
            authority_refs.append(row[0])
        basis = bool(row) or admission.has_content_basis(out["meta"])
        record("output_basis", "pass" if basis else "unknown", out["memory_id"])
        if change["risk"] == "R3" or out["meta"].get("scope") == "global":
            record("risk_authority", "pass" if row else "fail",
                   f"{out['memory_id']}: output revision requires authority confirmation")
    authority_ref = "; ".join(dict.fromkeys(authority_refs))

    report_id = "AR-" + hashlib.sha256(
        contracts.canonical_json({"c": change_id, "v": VALIDATOR_ID,
                                  "checks": checks}).encode("utf-8")).hexdigest()[:20]
    result = overall
    conn.execute(
        "INSERT OR REPLACE INTO admission_reports VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (report_id, change["base_heads"][0]["memory_id"] if change["base_heads"] else "",
         change["base_heads"][0]["content_revision"] if change["base_heads"] else "",
         change_id, "", change["risk"], result,
         json.dumps(checks, ensure_ascii=False), "{}", authority_ref, VALIDATOR_ID,
         "", _now()))
    if result == "pass":
        _changes.set_state(conn, change_id, "ready", reason="validation passed")
    elif result == "fail" and any(c["check"] == "base_current" and c["result"] == "fail"
                                  for c in checks):
        _changes.set_state(conn, change_id, "needs_rebase", reason="base head moved")
    elif result == "fail":
        _changes.set_state(conn, change_id, "needs_review", reason="validation failed")
    return {"change_id": change_id, "validation_id": report_id, "result": result,
            "checks": checks, "authority_ref": authority_ref}
