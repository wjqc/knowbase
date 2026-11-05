"""V2-01 治理安全门与观测口径：不可信反馈不能晋升。

验收反例（任务书 V2-01 §10/§11）：
- read + completed（绑定 read 回执 + Agent 自报任务完成）不晋升 staging 卡；
- adopted + 自报 completed/helpful 不晋升 active once 卡；
- 无 task_result 两工具标签旁路默认关闭；
- 自填 verification_source=independent 不获得任何提权；
- 自报 incorrect/outdated 不立即置 stale；
- 同请求重放 10 次只产生 1 条事件；
- frontmatter 反馈计数默认不参与检索排序；
- 正式 MCP 读回治理状态未变。
"""
import json

from knowbase import config, index, store, versioned_feedback, writer
from knowbase.server import context_impl, read_impl, feedback_impl
from knowbase.mutations import _compute_revision

BODY = ("## 结论\n已结算订单不能撤回。\n## 解决的问题\n避免结算状态被修改。\n"
        "## 适用条件\n订单完成结算。\n## 不适用条件\n草稿订单。\n"
        "## 可执行动作\n拒绝撤回请求。\n## 关键证据\n需求 PRD-4。\n"
        "## 验证情况\n使用场景测试验证。\n## 未知与待确认\n无。\n")


def governed_card(repo):
    result = writer.create(repo, "standard", "结算订单撤回标准", BODY,
                           scope="orders", provenance="PRD-4 §2")
    assert not result.get("error"), result
    return result["entity_id"]


def bound_read_feedback(repo, mid, task, host, *, outcome="helpful",
                        task_result="completed", verification_source="agent_report",
                        request_id=None):
    """正式入口链：context → read_impl（read 回执）→ 绑定反馈。"""
    session = "s-" + task
    json.loads(context_impl(session_id=session, event_id="lookup", scope="orders",
                            task_id=task, current_goal="验证订单规则", host_profile=host))
    read_impl(mid, session_id=session, event_id="read-" + task,
              context_event_id="lookup", host_profile=host)
    meta, body, _ = store.load(repo, mid)
    conn = index.connect(repo)
    try:
        return versioned_feedback.apply(
            repo, conn, mid, outcome, "tool-" + host,
            expected_content_revision=_compute_revision(meta, body),
            use_id=_use_id(conn, session), task_outcome_id="out-" + task,
            task_result=task_result, verification_source=verification_source,
            request_id=request_id or ("req-" + task))
    finally:
        conn.close()


def _use_id(conn, session):
    return conn.execute("SELECT use_id FROM knowledge_uses WHERE session_id=?",
                        (session,)).fetchone()[0]


def test_read_completed_does_not_promote_staging_by_default(memory_repo):
    """read + completed 三次跨实例验证：默认全部仅观察，卡留在 staging。"""
    mid = governed_card(memory_repo)
    for n, host in enumerate(("agent-a", "agent-b", "agent-c")):
        r = bound_read_feedback(memory_repo, mid, f"task-{n}", host)
        assert not r.get("error"), r
        assert r["governance_applied"] is False
        assert r["unknown_reason"] == "trusted_validation_unavailable"
        meta, _, path = store.load(memory_repo, mid)
        assert path.parent.name == "staging" and meta["confidence"] != "verified"
    meta, _, _ = store.load(memory_repo, mid)
    assert meta["helpful_count"] == 3  # 观察照常积累，治理不动。


def activate_copy(repo, mid, card_type):
    """模拟历史已激活卡：把 staging 卡移入正式目录并重建索引（测试基建，非治理路径）。"""
    src = store.load(repo, mid)[2]
    target = store.mem_path(repo, card_type, mid)
    target.parent.mkdir(parents=True, exist_ok=True)
    src.replace(target)
    conn = index.connect(repo)
    meta, body, path = store.load(repo, mid)
    index.upsert(conn, meta, body, path, False)
    conn.commit()
    conn.close()


def test_adopted_self_report_does_not_promote_active_once(memory_repo):
    """adopted/read + 自报 completed ×3：once 卡不晋升（伪 independent 同样无效）。"""
    mid = governed_card(memory_repo)
    activate_copy(memory_repo, mid, "standard")
    for n, host in enumerate(("agent-a", "agent-b", "agent-c")):
        r = bound_read_feedback(memory_repo, mid, f"adopt-{n}", host,
                                verification_source="independent")
        assert not r.get("error"), r
        assert r["governance_applied"] is False
        meta = store.load(memory_repo, mid)[0]
        assert meta["confidence"] == "once" and meta["status"] == "active"
        assert not meta.get("verified_content_revision")


def test_pseudo_independent_source_is_declaration_not_authentication(memory_repo):
    """verification_source=independent 只是来源声明：回执记录原值但不提权。"""
    mid = governed_card(memory_repo)
    r = bound_read_feedback(memory_repo, mid, "indep-1", "agent-a",
                            verification_source="independent")
    assert not r.get("error"), r
    assert r["observed"]["verification_source"] == "independent"
    assert r["observed"]["evidence_basis"] == "version_bound_agent_report"
    assert r["governance_applied"] is False
    assert store.load(memory_repo, mid)[0]["confidence"] != "verified"


def test_two_tool_labels_do_not_promote_by_default(memory_repo):
    """无 task_result 两工具标签旁路：默认关闭，跨工具 helpful 不晋升。"""
    mid = governed_card(memory_repo)
    activate_copy(memory_repo, mid, "standard")
    conn = index.connect(memory_repo)
    for host in ("tool-a", "tool-b"):
        r = versioned_feedback.apply(memory_repo, conn, mid, "helpful", host)
        assert not r.get("error"), r
        assert r["binding"] == "legacy_unbound" and r["governance_applied"] is False
    conn.close()
    meta = store.load(memory_repo, mid)[0]
    assert meta["confidence"] == "once" and meta["helpful_count"] == 0


def test_self_reported_incorrect_does_not_stale_by_default(memory_repo):
    """自报 incorrect：默认仅观察，active 不变 stale（S2 反例前置门）。"""
    mid = governed_card(memory_repo)
    r = bound_read_feedback(memory_repo, mid, "wrong-1", "agent-a",
                            outcome="incorrect", task_result="failed")
    assert not r.get("error"), r
    assert r["governance_applied"] is False
    meta = store.load(memory_repo, mid)[0]
    assert meta["status"] == "active" and "stale_since" not in meta
    assert meta["unhelpful_count"] == 1


def test_replay_ten_times_records_single_event(memory_repo):
    """同请求重放 10 次：只有 1 条 feedback 事件（幂等）。"""
    mid = governed_card(memory_repo)
    ids = set()
    for _ in range(10):
        r = bound_read_feedback(memory_repo, mid, "replay", "agent-a",
                                request_id="req-replay")
        assert not r.get("error"), r
        ids.add(r["receipt_id"])
    assert len(ids) == 1
    conn = index.connect(memory_repo)
    assert conn.execute("SELECT COUNT(*) FROM feedback_events").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM feedback_log").fetchone()[0] == 1
    conn.close()
    assert store.load(memory_repo, mid)[0]["helpful_count"] == 1


def test_feedback_counts_do_not_affect_ranking_by_default(memory_repo):
    """frontmatter 反馈计数默认不参与排序：同一卡计数抬升后得分不变。"""
    res = writer.create(memory_repo, "pitfall", "CRLF 行尾读视图不一致排查A", BODY,
                        scope="orders")
    assert not res.get("error"), res
    mid = res["entity_id"]
    conn = index.connect(memory_repo)
    before = {r["id"]: r["score"] for r in index.search(conn, "CRLF 行尾", origin="local")}
    assert mid in before
    # 落 helpful 计数（frontmatter 展示字段），模拟历史自报积累后重跑同一查询。
    meta, body, path = store.load(memory_repo, mid)
    meta["helpful_count"] = 40
    path.write_text(store.render(meta, body), encoding="utf-8")
    m2, b2, p2 = store.load(memory_repo, mid)
    index.upsert(conn, m2, b2, p2, p2.parent.name == "staging", commit=False)
    conn.commit()
    after = {r["id"]: r["score"] for r in index.search(conn, "CRLF 行尾", origin="local")}
    conn.close()
    assert mid in after
    # 计数从 0 → 40：若计数参与排序（0.5~1.5 系数），得分必然抬升。
    assert abs(after[mid] - before[mid]) < 1e-9, (before[mid], after[mid])


def test_formal_mcp_readback_governance_unchanged(memory_repo):
    """正式入口端到端：feedback 后通过 MCP 形状读回，治理字段未变。"""
    mid = governed_card(memory_repo)
    bound_read_feedback(memory_repo, mid, "mcp-readback", "agent-a")
    # 正式 read 读回（validate_only 语义外的默认读路径）。
    session = "s-mcp"
    json.loads(context_impl(session_id=session, event_id="lookup2", scope="orders",
                            task_id="mcp-task", current_goal="读取验证",
                            host_profile="agent-a"))
    text = read_impl(mid, session_id=session, event_id="read-back",
                     context_event_id="lookup2", host_profile="agent-a")
    assert mid in text
    meta, _, path = store.load(memory_repo, mid)
    assert meta["confidence"] == "once"
    assert path.parent.name == "staging"
    assert not meta.get("verified_content_revision")
    assert not meta.get("last_verified")


def test_unknown_reason_not_counted_as_evaluable_sample(memory_repo):
    """unknown 不进评测最低样本：执行派生数据集把未判定单列。"""
    from knowbase import execution_eval
    mid = governed_card(memory_repo)
    bound_read_feedback(memory_repo, mid, "eval-1", "agent-a")  # read+completed → 仅观察
    conn = index.connect(memory_repo)
    rows, stats = execution_eval.derive_dataset(conn)
    conn.close()
    # 无 adopted use → 无正/负样本；unknown/skipped 单列，不凑样本数。
    assert stats["positive"] == 0
    assert all(r["labels"]["status"] != "positive" for r in rows), stats
