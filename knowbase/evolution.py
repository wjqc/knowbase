"""版本绑定的确定性演进策略；模型提交证据，服务端去重并决定状态。"""
from . import config


def evidence(conn, mid, revision, use, task_key, outcome, task_result):
    """仅当前内容版本；一项任务重复反馈不增加证据，失败/冲突阻止晋升。"""
    rows = conn.execute(
        "SELECT f.task_key,f.outcome,u.host_profile,u.session_id,t.result "
        "FROM feedback_events f JOIN knowledge_uses u ON u.use_id=f.use_id "
        "LEFT JOIN task_outcomes t ON t.task_outcome_id=f.task_outcome_id "
        "WHERE f.memory_id=? AND f.content_revision=? AND f.binding='version_task' "
        "AND f.state='applied'", (mid, revision)).fetchall()
    rows.append((task_key, outcome, use[0], use[1], task_result))
    positive, negative, sessions = set(), set(), set()
    blocked = False
    for key, result, host, session, completion in rows:
        if result != "helpful" or completion in ("failed", "abandoned"):
            blocked = True
            if result == "not_helpful":
                negative.add(key)
        elif completion == "completed":
            positive.add(key)
            sessions.add(session)
    return {"helpful_tasks": len(positive), "sessions": len(sessions),
            "negative_tasks": len(negative), "blocked": blocked}


def decide(conn, mid, revision, meta, use, task_key, outcome, task_result, *, staged=False):
    policy = config.load_config().get("evolution", {})
    counts = evidence(conn, mid, revision, use, task_key, outcome, task_result)
    counts["promote"] = counts["retire"] = False
    if not policy.get("enabled", True):
        return counts
    counts["retire"] = outcome in ("incorrect", "outdated") or \
        counts["negative_tasks"] >= max(2, int(policy.get("retire_not_helpful_tasks", 2)))
    promotable = meta.get("status") == "active" and (not staged or (
        meta.get("type") == "bizrule" and bool(meta.get("provenance")) and
        policy.get("auto_activate_bizrules", True)))
    counts["promote"] = bool(promotable and outcome == "helpful" and not counts["blocked"]
        and counts["helpful_tasks"] >= max(2, int(policy.get("min_helpful_tasks", 3)))
        and counts["sessions"] >= max(2, int(policy.get("min_sessions", 2))))
    return counts
