"""首次准入与权威确认门（V2-08）。

staging 不需要先被默认检索命中或被真实业务采用即可准入（任务书 V2-08）：
准入检查的是结构/脱敏/来源/范围/风险/权威，不是使用流量。输出 admission
report 绑定具体 revision；通过只代表 validated/once 可发布，不自动 verified。

权威门（设计 §7.2）：
- standard / global preference：一次性权威确认绑定具体版本与 scope——
  authority_ref 必填（需求编号/负责人确认记录），多个 Agent 赞成不算。
- bizrule：provenance + rule_context（既有 lint 已强制），现代码行为不
  单独证明业务真值。
- project preference：明确用户指令即可（来源声明）。
- 人工 promote/verify 也必须落同一报告（不信 CLI 参数自填 human）。
"""
import hashlib
import json
from datetime import datetime, timezone

from .. import config, store
from . import contracts

ADMISSION_VALIDATOR_ID = "lifecycle-admission-v1"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def classify_risk(card_type: str, change_relation: str = "NEW") -> str:
    """类型 × 关系 → 风险分级（设计 §6.1）。"""
    if card_type in ("standard",) or (card_type == "preference" and change_relation != "NEW"):
        return "R3" if card_type == "standard" else "R3"
    if card_type == "bizrule" or change_relation in ("CORRECT", "REPLACE", "MERGE", "SPLIT"):
        return "R2"
    if change_relation in ("ENRICH",):
        return "R1"
    return "R1"


def evaluate_admission(conn, repo, memory_id: str, content_revision: str, *,
                       authority_ref: str = "", change_id: str = "",
                       operator: str = "", note: str = "") -> dict:
    """评估一张卡（具体 revision）的首次准入/修订准入。

    返回 {result: pass|fail|unknown, checks, risk, card_type}；同时落
    admission_reports（幂等键：memory_id+revision+change_id+report 内容）。
    审核超时不默认批准：无 authority_ref 的 strict 类型就是 fail，保持待审。
    """
    meta, body, path = store.load(repo, memory_id)
    if not meta:
        return {"error": "NOT_FOUND", "memory_id": memory_id}
    from ..mutations import _compute_revision
    if _compute_revision(meta, body) != content_revision:
        return {"error": "REVISION_CONFLICT",
                "current": _compute_revision(meta, body)}
    card_type = meta.get("type", "")
    scope = meta.get("scope", "")
    lint_errors = store.lint(meta, body, repo)
    checks = []

    def record(check, result, detail=""):
        checks.append({"check": check, "result": result, "detail": detail})

    record("structure", "pass" if not lint_errors else "fail",
           ";".join(lint_errors[:5]) if lint_errors else "eight-section lint clean")
    overall = "pass" if not lint_errors else "fail"

    # 来源可追溯。
    has_source = bool(str(meta.get("source") or "").strip())
    has_evidence = bool(meta.get("evidence") or meta.get("code_refs")
                        or meta.get("source_refs"))
    record("source_traceable", "pass" if (has_source or has_evidence) else "unknown",
           "source/evidence present" if (has_source or has_evidence)
           else "no source or evidence reference")
    if not (has_source or has_evidence) and overall == "pass":
        overall = "unknown"

    # 类型/权威门。
    if card_type in ("standard", "bizrule") and not str(meta.get("provenance") or "").strip():
        record("provenance", "fail", f"{card_type} requires provenance")
        overall = "fail"
    elif card_type == "bizrule":
        record("provenance", "pass", "provenance present")
    if card_type == "standard" or (card_type == "preference" and scope == "global"):
        if not authority_ref:
            record("authority_confirmation", "fail",
                   "standard/global preference requires one-time authority "
                   "confirmation bound to this revision and scope")
            overall = "fail"
        else:
            record("authority_confirmation", "pass", f"authority_ref={authority_ref}")
    # project preference：明确用户指令可作为授权证据（来源声明，不自动扩大 global）。
    if card_type == "preference" and scope != "global":
        record("authority_confirmation", "pass" if has_source else "unknown",
               "project preference: explicit user instruction via source")

    risk = classify_risk(card_type)
    report_id = "ADM-" + hashlib.sha256(contracts.canonical_json({
        "m": memory_id, "r": content_revision, "c": change_id, "a": authority_ref,
        "checks": checks}).encode("utf-8")).hexdigest()[:20]
    conn.execute(
        "INSERT OR REPLACE INTO admission_reports VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (report_id, memory_id, content_revision, change_id, card_type, risk, overall,
         json.dumps(checks, ensure_ascii=False), "{}", authority_ref,
         ADMISSION_VALIDATOR_ID, "", _now()))
    conn.commit()
    return {"report_id": report_id, "memory_id": memory_id,
            "content_revision": content_revision, "card_type": card_type,
            "risk": risk, "result": overall, "checks": checks,
            "authority_ref": authority_ref,
            "note": "pass = validated/once publishable; never auto-verified"}


def pending_admissions(conn, repo, *, limit: int = 50) -> list[dict]:
    """待准入清单：staging 卡与历史 stale 复验候选（按任务相关性/年龄调度
    的输入；选择策略在 worker/CLI 层）。staging 无需先被检索命中。"""
    out = []
    errors = []
    for meta, body, path in store.iter_all(repo, include_staging=True, errors=errors):
        staging = path.parent.name == "staging"
        if not staging and meta.get("status") != "stale":
            continue
        from ..mutations import _compute_revision
        revision = _compute_revision(meta, body)
        admitted = conn.execute(
            "SELECT COUNT(*) FROM admission_reports WHERE memory_id=? AND "
            "content_revision=? AND result='pass'",
            (meta["id"], revision)).fetchone()[0]
        out.append({"memory_id": meta["id"], "title": meta.get("title", ""),
                    "type": meta.get("type", ""), "scope": meta.get("scope", ""),
                    "staging": staging, "status": meta.get("status", ""),
                    "content_revision": revision, "already_admitted": bool(admitted)})
    return out[:limit]


def authority_confirm(conn, repo, change_id: str, memory_id: str,
                      content_revision: str, authority_ref: str) -> dict:
    """权威确认：把具体 change+版本 的确认落成准入报告。

    authority_ref 是确认出处（需求单/负责人记录）；操作者身份由调用方
    （CLI/受信 worker）保证，本函数不以参数自称 human。"""
    if not str(authority_ref).strip():
        return {"error": "AUTHORITY_REQUIRED"}
    return evaluate_admission(conn, repo, memory_id, content_revision,
                              authority_ref=authority_ref, change_id=change_id,
                              note="authority confirmation")
