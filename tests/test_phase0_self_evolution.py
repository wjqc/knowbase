"""Phase 0 自进化回归测试：staging 统一自动激活出口 + 已生效卡修订即降权 + stale 自动复活。

全部场景零人工命令：测试中不调用任何 cmd_promote / cmd_verify / cmd_revise /
cmd_archive，激活、修订、复活均由 Agent 任务流（context → read → bound feedback）驱动。
"""
import json

from knowbase import config, index, store, versioned_feedback, writer
from knowbase.server import context_impl, read_impl
from knowbase.mutations import _compute_revision

BODY = ("## 结论\n已结算订单不能撤回。\n## 解决的问题\n避免结算状态被修改。\n"
        "## 适用条件\n订单完成结算。\n## 不适用条件\n草稿订单。\n"
        "## 可执行动作\n拒绝撤回请求。\n## 关键证据\n需求 PRD-4。\n"
        "## 验证情况\n使用场景测试验证。\n## 未知与待确认\n无。\n")


def standard_card(repo, *, provenance="PRD-4 §2"):
    result = writer.create(repo, "standard", "结算订单撤回标准", BODY,
                           scope="orders", provenance=provenance)
    assert not result.get("error"), result
    assert result["staging"] is True
    return result["entity_id"]


def validate(repo, mid, task, host, *, outcome="helpful", task_result="completed"):
    """一次绑定 read 回执的真实任务验证：context → read → bound feedback。"""
    session = "s-" + task
    result = json.loads(context_impl(session_id=session, event_id="lookup", scope="orders",
                                     task_id=task, current_goal="验证订单规则",
                                     host_profile=host))
    if (store.load(repo, mid)[2].parent.name == "staging" and
            config.load_config()["evolution"].get("auto_activate_bizrules", True)):
        assert mid in {c["id"] for c in result.get("validation_candidates", [])}, result
    text = read_impl(mid, session_id=session, event_id="validation-read",
                     context_event_id="lookup", host_profile=host)
    assert "read_use_id=" in text, text
    meta, body, _ = store.load(repo, mid)
    conn = index.connect(repo)
    use = conn.execute("SELECT use_id FROM knowledge_uses WHERE session_id=?", (session,)).fetchone()[0]
    try:
        return versioned_feedback.apply(repo, conn, mid, outcome, "tool-" + host,
            expected_content_revision=_compute_revision(meta, body), use_id=use,
            task_outcome_id="out-" + task, task_result=task_result,
            request_id="req-" + task)
    finally:
        conn.close()


def searchable(repo, mid, query="结算订单撤回"):
    conn = index.connect(repo)
    try:
        return mid in {r["id"] for r in index.search(conn, query, origin="local")}
    finally:
        conn.close()


def activate(repo, mid, prefix="act"):
    for n, host in enumerate(("agent-a", "agent-b", "agent-c")):
        result = validate(repo, mid, f"{prefix}-{n}", host)
        assert not result.get("error"), result


def test_staged_standard_auto_activates_after_three_instance_validations(memory_repo):
    """0-A E2E：3 个不同 Agent 实例的验证把 staged standard 卡推到 verified，全程零人工命令。"""
    mid = standard_card(memory_repo)
    assert store.load(memory_repo, mid)[2].parent.name == "staging"
    assert not searchable(memory_repo, mid)
    for n, host in enumerate(("agent-a", "agent-b", "agent-c")):
        result = validate(memory_repo, mid, f"task-{n}", host)
        assert not result.get("error"), result
        if n < 2:
            # 达标前：仍在 staging，且绝不进入默认检索。
            assert store.load(memory_repo, mid)[2].parent.name == "staging"
            assert not searchable(memory_repo, mid)
    meta, _, path = store.load(memory_repo, mid)
    assert meta["confidence"] == "verified" and path.parent.name == "standards"
    assert searchable(memory_repo, mid)


def test_staged_standard_without_provenance_stays_staging(memory_repo):
    """0-A 负向：无 provenance 的 staged standard 卡即使攒够证据也不自动激活。"""
    mid = standard_card(memory_repo, provenance=None)
    for n, host in enumerate(("agent-a", "agent-b", "agent-c")):
        result = validate(memory_repo, mid, f"task-{n}", host)
        assert not result.get("error"), result
    meta, _, path = store.load(memory_repo, mid)
    assert path.parent.name == "staging" and meta["confidence"] != "verified"
    assert meta["helpful_count"] == 3  # 证据照常积累，只是未达激活条件。


def test_auto_activation_disabled_for_standard(memory_repo):
    """0-A 负向：自动激活开关关闭时，staged standard 卡的验证被拒绝，留在 staging。"""
    mid = standard_card(memory_repo)
    cfg = json.loads(config.CONFIG_PATH.read_text())
    cfg["evolution"] = {"auto_activate_bizrules": False}
    config.CONFIG_PATH.write_text(json.dumps(cfg))
    result = validate(memory_repo, mid, "blocked", "agent-a")
    assert result["error"] == "VALIDATION_RESULT_REQUIRED"
    assert store.load(memory_repo, mid)[2].parent.name == "staging"


def test_governed_update_demotes_verified_and_reearns(memory_repo):
    """0-B：Agent 修订已生效卡不再被 GOVERNANCE_DENIED 拒绝，改版即降权，凭新版本证据重新晋升。"""
    mid = standard_card(memory_repo)
    activate(memory_repo, mid)
    meta, _, _ = store.load(memory_repo, mid)
    assert meta["confidence"] == "verified"
    revised = BODY.replace("已结算订单不能撤回。", "已结算订单不能撤回，特殊通道需复核。")
    result = writer.update(memory_repo, mid, body=revised, push=False)
    assert not result.get("error"), result
    meta, _, _ = store.load(memory_repo, mid)
    assert meta["confidence"] == "once" and meta["verified_content_revision"] == ""
    assert meta["last_verified"] == "" and meta["helpful_count"] == 0 and meta["unhelpful_count"] == 0
    assert meta["status"] == "active"
    for n, host in enumerate(("agent-a", "agent-b", "agent-c")):
        result = validate(memory_repo, mid, f"re-{n}", host)
        assert not result.get("error"), result
    assert store.load(memory_repo, mid)[0]["confidence"] == "verified"


def test_agent_cannot_set_governance_fields_on_governed_card(memory_repo):
    """0-B 防线：修订放开后，治理字段仍由服务端状态机管理，调用方不可直接指定。"""
    mid = standard_card(memory_repo)
    activate(memory_repo, mid)
    before = store.load(memory_repo, mid)[0]
    assert before["confidence"] == "verified"
    for field, value in (("confidence", "verified"), ("status", "archived")):
        result = writer.update(memory_repo, mid, push=False, **{field: value})
        assert result["error"] == "GOVERNANCE_FIELDS_RESERVED", result
    after = store.load(memory_repo, mid)[0]
    assert after["confidence"] == "verified" and after["status"] == "active"


def test_stale_revive_via_revision_and_new_evidence(memory_repo):
    """0-B E2E（严格语义）：stale 卡经 Agent 修订后仍 stale 不可见，凭新版本证据重新 verified，旧版本证据无法复活。"""
    mid = standard_card(memory_repo)
    activate(memory_repo, mid)
    # 一条版本绑定的 incorrect 反馈 → stale。
    result = validate(memory_repo, mid, "bad-one", "agent-a",
                      outcome="incorrect", task_result="failed")
    assert not result.get("error"), result
    meta, _, _ = store.load(memory_repo, mid)
    assert meta["status"] == "stale"
    assert not searchable(memory_repo, mid)
    # 在修订前先留一个绑定旧版本的 read 使用：修订后用它反馈必须被版本冲突拒绝。
    session = "s-old"
    json.loads(context_impl(session_id=session, event_id="lookup", scope="orders",
                            task_id="old-task", current_goal="验证订单规则",
                            host_profile="agent-a"))
    text = read_impl(mid, session_id=session, event_id="old-read",
                     context_event_id="lookup", host_profile="agent-a")
    assert "read_use_id=" in text, text
    old_meta, old_body, _ = store.load(memory_repo, mid)
    old_rev = _compute_revision(old_meta, old_body)
    conn = index.connect(memory_repo)
    old_use = conn.execute("SELECT use_id FROM knowledge_uses WHERE session_id=?",
                           (session,)).fetchone()[0]
    conn.close()
    # Agent 修订 stale 卡（严格语义）：仍 stale 不可见，置 revalidation_pending。
    revised = BODY.replace("已结算订单不能撤回。", "已结算订单不能撤回，特殊通道需复核。")
    result = writer.update(memory_repo, mid, body=revised, push=False)
    assert not result.get("error"), result
    meta, _, _ = store.load(memory_repo, mid)
    assert meta["status"] == "stale" and meta["confidence"] == "once"
    assert meta.get("revalidation_pending") is True
    assert not searchable(memory_repo, mid)
    # 旧版本证据无法复活新版本。
    conn = index.connect(memory_repo)
    try:
        stale_result = versioned_feedback.apply(
            memory_repo, conn, mid, "helpful", "tool-agent-a",
            expected_content_revision=old_rev, use_id=old_use,
            task_outcome_id="out-old-revival", task_result="completed",
            request_id="req-old-revival")
    finally:
        conn.close()
    assert stale_result["error"] == "REVISION_CONFLICT", stale_result
    # 新版本证据重新晋升，全程零人工命令：晋升时 status 回 active、标记清除。
    for n, host in enumerate(("agent-a", "agent-b", "agent-c")):
        result = validate(memory_repo, mid, f"revive-{n}", host)
        assert not result.get("error"), result
    meta, _, _ = store.load(memory_repo, mid)
    assert meta["confidence"] == "verified" and meta["status"] == "active"
    assert "revalidation_pending" not in meta
    assert searchable(memory_repo, mid)


def test_unrevised_stale_card_cannot_promote_on_feedback(memory_repo):
    """0-B 负向：未修订的 stale 卡即使收到反馈也不晋升——证据照常积累，但复活通道未打开。"""
    mid = standard_card(memory_repo)
    activate(memory_repo, mid)
    result = validate(memory_repo, mid, "bad-one", "agent-a",
                      outcome="incorrect", task_result="failed")
    assert not result.get("error"), result
    meta, _, _ = store.load(memory_repo, mid)
    assert meta["status"] == "stale"
    before = meta["helpful_count"]
    for n, host in enumerate(("agent-a", "agent-b", "agent-c")):
        result = validate(memory_repo, mid, f"unrev-{n}", host)
        assert not result.get("error"), result
    meta, _, _ = store.load(memory_repo, mid)
    assert meta["helpful_count"] == before + 3  # 证据照常积累。
    # 注意：降 stale 只改 status，不动 confidence；可见性由 status 决定，
    # 因此这里只断言 status 仍为 stale 且不可检索——这正是复活通道未打开的证据。
    assert meta["status"] == "stale"
    assert "revalidation_pending" not in meta  # 反馈本身不打开复活通道。
    assert not searchable(memory_repo, mid)


def test_content_unchanged_revision_does_not_open_revival(memory_repo):
    """0-B 负向：无内容变化的"修订"不置标记，stale 卡仍不可复活。"""
    mid = standard_card(memory_repo)
    activate(memory_repo, mid)
    result = validate(memory_repo, mid, "bad-one", "agent-a",
                      outcome="incorrect", task_result="failed")
    assert not result.get("error"), result
    meta, _, _ = store.load(memory_repo, mid)
    assert meta["status"] == "stale"
    _, current_body, _ = store.load(memory_repo, mid)
    result = writer.update(memory_repo, mid, body=current_body, push=False)
    assert not result.get("error"), result
    meta, _, _ = store.load(memory_repo, mid)
    assert meta["status"] == "stale" and "revalidation_pending" not in meta
    for n, host in enumerate(("agent-a", "agent-b", "agent-c")):
        result = validate(memory_repo, mid, f"same-{n}", host)
        assert not result.get("error"), result
    meta, _, _ = store.load(memory_repo, mid)
    assert meta["status"] == "stale"
    assert "revalidation_pending" not in meta
    assert not searchable(memory_repo, mid)
