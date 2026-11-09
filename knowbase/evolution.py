"""版本绑定的确定性演进策略；模型提交证据，服务端去重并决定状态。

V2-01 安全门：反馈驱动的自动晋升/降级默认关闭。read+completed、Agent
自报 helpful、自填 verification_source=independent 均只是来源声明，不是
可信执行验证；在可信验证器（V2-11/12）接入前，decide() 一律返回
promote=retire=False 并给出 blocked_reason，反馈照常落盘为观察。
"""
from . import config

POLICY_REVISION = "lifecycle-v2-gate.1"


def trusted_promotion_available() -> bool:
    """反馈驱动晋升/降级是否具备可信执行证据来源。

    默认 False：V2-01 只封旁路不接新证据。evolution.trusted_promotion_enabled
    是隔离机制测试的过渡开关；V2-12 治理引擎改为消费核验过的 L3/L4
    证据事件后，本函数由证据可用性取代配置开关。"""
    return bool(config.load_config().get("evolution", {}).get("trusted_promotion_enabled", False))


def _aggregate(rows):
    """证据聚合（纯函数）：仅当前内容版本；一项任务重复反馈不增加证据。

    negative_evidence_tasks 为负面证据任务数（含 not_helpful/incorrect/outdated），
    供矛盾裁决使用；晋升/淘汰口径（negative_tasks 仅计 not_helpful）保持不变。
    """
    positive, negative, sessions = set(), set(), set()
    negative_evidence = set()
    blocked = False
    for key, result, host, session, completion in rows:
        if result != "helpful" or completion in ("failed", "abandoned"):
            blocked = True
            if result == "not_helpful":
                negative.add(key)
            if result in ("not_helpful", "incorrect", "outdated"):
                negative_evidence.add(key)
        elif completion == "completed":
            positive.add(key)
            sessions.add(session)
    return {"helpful_tasks": len(positive), "sessions": len(sessions),
            "negative_tasks": len(negative), "negative_evidence_tasks": len(negative_evidence),
            "blocked": blocked}


def evidence_for_revision(conn, mid, revision):
    """仅查库内已落盘证据（不附加假设行）：供矛盾裁决等只读判定使用。"""
    rows = conn.execute(
        "SELECT f.task_key,f.outcome,u.host_profile,u.session_id,t.result "
        "FROM feedback_events f JOIN knowledge_uses u ON u.use_id=f.use_id "
        "LEFT JOIN task_outcomes t ON t.task_outcome_id=f.task_outcome_id "
        "WHERE f.memory_id=? AND f.content_revision=? AND f.binding='version_task' "
        "AND f.state='applied'", (mid, revision)).fetchall()
    return _aggregate(rows)


def evidence(conn, mid, revision, use, task_key, outcome, task_result):
    """仅当前内容版本；一项任务重复反馈不增加证据，失败/冲突阻止晋升。"""
    rows = conn.execute(
        "SELECT f.task_key,f.outcome,u.host_profile,u.session_id,t.result "
        "FROM feedback_events f JOIN knowledge_uses u ON u.use_id=f.use_id "
        "LEFT JOIN task_outcomes t ON t.task_outcome_id=f.task_outcome_id "
        "WHERE f.memory_id=? AND f.content_revision=? AND f.binding='version_task' "
        "AND f.state='applied'", (mid, revision)).fetchall()
    rows.append((task_key, outcome, use[0], use[1], task_result))
    agg = _aggregate(rows)
    agg.pop("negative_evidence_tasks", None)
    return agg


def activation_policy() -> dict:
    """P4 显式激活策略：团队允许自动激活的类型/范围与策略版本。

    allowed_types/allowed_scopes 为 None 表示不限制（沿用已有授权范围，
    即 decide() 原有的 promotable 门）；显式设置后才收紧。策略只 gate
    晋升/激活，不影响证据收集与候选存储（反馈仍正常落盘）。
    """
    raw = config.load_config().get("evolution", {}).get("activation_policy") or {}
    return {"allowed_types": raw.get("allowed_types"),
            "allowed_scopes": raw.get("allowed_scopes"),
            "policy_revision": str(raw.get("policy_revision", "1"))}


def decide(conn, mid, revision, meta, use, task_key, outcome, task_result, *, staged=False):
    # auto_activate_bizrules 现为全类型统一自动激活出口的总开关（历史名称保留）。
    policy = config.load_config().get("evolution", {})
    counts = evidence(conn, mid, revision, use, task_key, outcome, task_result)
    counts["promote"] = counts["retire"] = False
    counts["policy_revision"] = POLICY_REVISION
    if not policy.get("enabled", True):
        counts["blocked_reason"] = "evolution_disabled"
        return counts
    if not trusted_promotion_available():
        # V2-01：无可信执行验证来源，反馈只作观察；证据计数继续返回供展示。
        counts["blocked_reason"] = "trusted_validation_unavailable"
        return counts
    counts["retire"] = outcome in ("incorrect", "outdated") or \
        counts["negative_tasks"] >= max(2, int(policy.get("retire_not_helpful_tasks", 2)))
    # 严格语义的复活通道：仅当 stale 卡带有修订标记（内容已修订产生新版本）
    # 且不在 staging 目录时，才放开 status 要求；未修订的 stale 卡永远不
    # 满足 promotable。标记本身不降低阈值：仍需独立 helpful 任务 ≥ 下限、
    # 独立会话 ≥ 下限且零阻断。
    revalidation = (meta.get("status") == "stale"
                    and bool(meta.get("revalidation_pending")) and not staged)
    promotable = (meta.get("status") == "active" and (not staged or (
        bool(meta.get("provenance")) and
        policy.get("auto_activate_bizrules", True)))) or revalidation
    counts["promote"] = bool(promotable and outcome == "helpful" and not counts["blocked"]
        and counts["helpful_tasks"] >= max(2, int(policy.get("min_helpful_tasks", 3)))
        and counts["sessions"] >= max(2, int(policy.get("min_sessions", 2))))
    # P4 激活策略门：未授权类型/范围不自动激活，但证据已在反馈路径落盘、
    # 候选仍可存储——策略只 gate 晋升，不扩大也不切断证据收集。
    if counts["promote"]:
        ap = activation_policy()
        if ap["allowed_types"] is not None and meta.get("type") not in ap["allowed_types"]:
            counts["promote"] = False
        elif ap["allowed_scopes"] is not None and meta.get("scope") not in ap["allowed_scopes"]:
            counts["promote"] = False
    return counts
