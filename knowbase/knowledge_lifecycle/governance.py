"""证据驱动的确定性治理投影（V2-12）。

决策函数：decision = f(已验证内容版本, 有效授权证据集合, 适用环境, policy)。
- 输入只认 evidence_events 中未撤销、trust_basis=registered-adapter 的
  L3/L4 事件；L0/L1/L2 永远零治理票。
- 独立性按 logical_task_key 去重：同任务 100 次重放仍 1 个支持任务；
  同输入/同运行共享 independence_group 不扩票；unknown: 前缀的任务键
  （宿主无原生任务键）不参与独立性计数（W0-2 修正：此前 decide() 未过滤，
  unknown 最多贡献 1 任务 + 1 组）。
- 反证门：同版本存在未裁决的 trusted fail（同 claim/条件）→ 不晋升，
  且可信反证触发隔离（quarantined 兼容映射 = stale + 隔离原因）。
- 撤销重算：evidence.revoked 目标事件剔除后重算；撤销不自动复活。
- 同输入同政策输出逐字一致（input_set_digest 决定）。

投影落地（兼容镜像）：confidence once→verified、quality_state 字段、
status→stale（隔离）；治理字段不产生新 content revision。
"""
import hashlib
import json
from datetime import datetime, timezone

from .. import index, store
from . import contracts

POLICY_REVISION = "lifecycle-governance-v1"
MIN_INDEPENDENT_TASKS = 3
MIN_INDEPENDENCE_GROUPS = 2


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _revoked_ids(conn) -> set[str]:
    return {r[0] for r in conn.execute(
        "SELECT revokes_event_id FROM evidence_events WHERE event_type='evidence.revoked' "
        "AND revokes_event_id!=''")}


def effective_evidence(conn, memory_id: str, revision: str) -> list[dict]:
    """当前版本的有效证据集合：trusted L3/L4、未撤销、未本地隔离。"""
    revoked = _revoked_ids(conn)
    rows = conn.execute(
        "SELECT event_id,level,polarity,logical_task_key,independence_group,claim_id,"
        "trust_basis,payload_json,occurred_at FROM evidence_events WHERE "
        "subject_revision=? AND level IN ('L3','L4') AND import_state IN ('local','imported')",
        (f"{memory_id}@content-v1:{revision}",)).fetchall()
    out = []
    for row in rows:
        if row[0] in revoked:
            continue
        out.append({"event_id": row[0], "level": row[1], "polarity": row[2],
                    "logical_task_key": row[3], "independence_group": row[4],
                    "claim_id": row[5], "trust_basis": row[6],
                    "payload": json.loads(row[7] or "{}"), "occurred_at": row[8]})
    return out


def _counts_toward_independence(event: dict) -> bool:
    """unknown 身份不参与独立性计数（contracts.logical_task_key 的约定）。

    任务键与独立组任一带 unknown: 前缀即不计；本地派生事件的组等于任务键，
    故两者都要查。反证门不受此限（见 decide）：可信 fail 的治理效力来自
    验证器登记与签名信任，不是任务身份。
    """
    key = event.get("logical_task_key") or ""
    group = event.get("independence_group") or ""
    return bool(key) and not key.startswith("unknown:") \
        and not group.startswith("unknown:")


def decide(conn, memory_id: str, revision: str) -> dict:
    """确定性决策（纯函数式，只读）。相同输入集合 digest 输出一致。"""
    events = effective_evidence(conn, memory_id, revision)
    basis = contracts.canonical_json(
        {"m": memory_id, "r": revision, "p": POLICY_REVISION,
         "e": sorted((e["event_id"], e["polarity"], e["logical_task_key"]) for e in events)})
    input_set_digest = hashlib.sha256(basis.encode("utf-8")).hexdigest()
    pass_tasks = {e["logical_task_key"] for e in events
                  if e["polarity"] == "positive" and _counts_toward_independence(e)}
    pass_groups = {e["independence_group"] for e in events
                   if e["polarity"] == "positive" and e["independence_group"]
                   and _counts_toward_independence(e)}
    unresolved_negative = [e for e in events if e["polarity"] == "negative"]
    if unresolved_negative:
        decision = "quarantine"
        reason = (f"{len(unresolved_negative)} trusted negative L3 on current revision "
                  "(same-version contradiction)")
    elif (len(pass_tasks) >= MIN_INDEPENDENT_TASKS
          and len(pass_groups) >= MIN_INDEPENDENCE_GROUPS):
        decision = "verified"
        reason = (f"{len(pass_tasks)} independent logical tasks, "
                  f"{len(pass_groups)} independence groups, zero unresolved negative")
    elif pass_tasks:
        decision = "validated"
        reason = f"{len(pass_tasks)} independent task(s); below promotion policy"
    else:
        decision = "unverified"
        reason = "no trusted L3 evidence on current revision"
    return {"memory_id": memory_id, "content_revision": revision,
            "decision": decision, "reason": reason,
            "input_set_digest": input_set_digest,
            "policy_revision": POLICY_REVISION,
            "independent_tasks": len(pass_tasks),
            "independence_groups": len(pass_groups),
            "unresolved_negative": len(unresolved_negative),
            "evidence_watermark": max((e["occurred_at"] for e in events), default="")}


def apply_decision(repo, conn, memory_id: str) -> dict:
    """重算并落地治理投影（兼容镜像字段；治理字段不动 content revision）。

    verified 晋升从本函数走（V2-01 已关反馈旁路）；隔离映射 status=stale +
    quarantine_reason，不删内容。"""
    from ..mutations import _compute_revision
    meta, body, path = store.load(repo, memory_id)
    if not meta:
        return {"error": "NOT_FOUND"}
    revision = _compute_revision(meta, body)
    decision = decide(conn, memory_id, revision)
    quality_state = {"unverified": "unverified", "validated": "validated",
                     "verified": "verified", "quarantine": "disputed"}[decision["decision"]]
    changed = False
    if decision["decision"] == "verified" and meta.get("confidence") != "verified":
        meta["confidence"] = "verified"
        meta["last_verified"] = datetime.now(timezone.utc).date().isoformat()
        meta["verified_content_revision"] = revision
        changed = True
    elif decision["decision"] != "verified" and meta.get("confidence") == "verified":
        # 支持不足（撤销后重算）→ 降回 once；不自动清理 verified 以外的字段。
        meta["confidence"] = "once"
        meta.pop("verified_content_revision", None)
        meta.pop("last_verified", None)
        changed = True
    if decision["decision"] == "quarantine" and meta.get("status") == "active":
        meta["status"] = "stale"
        meta["quarantine_reason"] = decision["reason"][:200]
        meta["stale_since"] = datetime.now(timezone.utc).date().isoformat()
        changed = True
    # 投影字段与现值比较后才写盘：调度场景（recompute_all 每轮全库重算）
    # 无变化不产生文件写入，避免全库卡片逐小时 git churn。
    if (meta.get("quality_state") != quality_state
            or meta.get("evidence_set_digest") != decision["input_set_digest"][:32]
            or meta.get("policy_revision") != POLICY_REVISION):
        changed = True
    meta["quality_state"] = quality_state
    meta["evidence_set_digest"] = decision["input_set_digest"][:32]
    meta["policy_revision"] = POLICY_REVISION
    if changed:
        from ..commit_adapter import _atomic_write
        _atomic_write(path, store.render(meta, body))
        index.upsert(conn, *store.load(repo, memory_id), commit=False)
        conn.commit()
    decision["quality_state"] = quality_state
    decision["applied"] = True
    return decision


def recompute_all(repo, conn) -> list[dict]:
    """全库重算（worker 调度/导入证据后调用）。"""
    decisions = []
    for meta, body, _path in store.iter_all(repo, include_staging=True):
        decisions.append(apply_decision(repo, conn, meta["id"]))
    return decisions
