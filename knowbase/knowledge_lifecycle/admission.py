"""首次准入与权威确认门（V2-08；统一生命周期 2026-11-09 修订）。

统一生命周期：类型只作检索分类，不决定准入。准入按统一的内容、变更与
共享风险判定：
- R1：项目内新增/非实质补充，且有出处或可核对依据 → 同请求直接发布；
- R2：改变已发布结论（CORRECT/REPLACE/MERGE/SPLIT）的项目内修订，
  或来源不足 → 待审 + 变更验证；
- R3：影响共享范围（scope=global）→ 待审 + 权威确认 + 变更验证。

staging 不需要先被默认检索命中或被真实业务采用即可准入（任务书 V2-08）：
准入检查的是结构/脱敏/来源/范围/风险/权威，不是使用流量。输出 admission
report 绑定具体 revision；通过只代表 validated/once 可发布，不自动 verified。

权威门（统一规则）：
- scope=global：一次性权威确认绑定具体版本与 scope——authority_ref 必填
  （需求编号/负责人确认记录），多个 Agent 赞成不算。
- 项目内：明确来源声明即可；来源不足只升风险进待审，不按类型硬拒。
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


def classify_risk(scope: str, change_relation: str = "NEW", *,
                  has_basis: bool = True) -> str:
    """统一风险分级：共享范围 × 变更关系（类型不参与）。

    has_basis 指来源/出处/证据任一存在；缺失的新增视为来源不足升 R2 待审。
    """
    if change_relation in ("CORRECT", "REPLACE", "MERGE", "SPLIT"):
        return "R3" if scope == "global" else "R2"
    if scope == "global":
        return "R3"
    if change_relation == "NEW" and not has_basis:
        return "R2"
    return "R1"


def has_content_basis(meta: dict) -> bool:
    """出处或可核对依据：真实来源声明、provenance、evidence/code_refs/source_refs。

    writer.create 在调用方未提供 source 时自动补的 `*:adhoc` 只是落盘兜底，
    不算依据（来源不足 → 待审）；显式提供的 agent:/human: 来源声明算出处。
    """
    src = str(meta.get("source") or "").strip()
    return bool((src and not src.endswith(":adhoc"))
                or str(meta.get("provenance") or "").strip()
                or meta.get("evidence") or meta.get("code_refs")
                or meta.get("source_refs"))


def evaluate_admission(conn, repo, memory_id: str, content_revision: str, *,
                       authority_ref: str = "", change_id: str = "",
                       operator: str = "", note: str = "") -> dict:
    """评估一张卡（具体 revision）的首次准入/修订准入。

    返回 {result: pass|fail|unknown, checks, risk, card_type}；同时落
    admission_reports（幂等键：memory_id+revision+change_id+report 内容）。
    审核超时不默认批准：global 范围无 authority_ref 就是 fail，保持待审。
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

    # 统一出处门：所有类型一致——有 provenance 记 pass，缺失记 unknown
    # （来源不足 → 待审，不按类型硬拒）。
    has_provenance = bool(str(meta.get("provenance") or "").strip())
    record("provenance", "pass" if has_provenance else "unknown",
           "provenance present" if has_provenance
           else "provenance absent: stays pending review, no type-based rejection")

    # 统一权威门：共享范围（global）需要权威确认，类型不参与。
    if scope == "global":
        if not authority_ref:
            record("authority_confirmation", "fail",
                   "global scope requires one-time authority confirmation "
                   "bound to this revision and scope")
            overall = "fail"
        else:
            record("authority_confirmation", "pass", f"authority_ref={authority_ref}")
    else:
        record("authority_confirmation", "pass" if (has_source or has_evidence) else "unknown",
               "project scope: explicit source via source/evidence")

    risk = classify_risk(scope, "NEW", has_basis=has_content_basis(meta))
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
