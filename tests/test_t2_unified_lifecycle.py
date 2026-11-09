"""T2 统一生命周期语义测试（类型不参与治理）。

计划 T2 通过条件：
- 相同输入条件下，不同类型走同一状态转换（staging 路由只看共享范围与依据）；
- 权威门只看共享范围（global），不按类型；
- provenance 缺失是"待审"（unknown），不是按类型硬拒；
- 绑定 read 验证通道对所有类型开放；
- 治理字段防线覆盖所有类型；revise CLI 不再限定三类型。
"""

import json

from knowbase import __main__ as cli, index, store, writer
from knowbase.knowledge_lifecycle import admission
from knowbase.migrations import apply_all
from knowbase.mutations import _compute_revision
from knowbase.server import feedback_impl, read_impl, use_impl, context_impl

BODY = ("## 结论\n受控样本结论。\n## 解决的问题\n验证统一语义。\n"
        "## 适用条件\n隔离库。\n## 不适用条件\n真实业务。\n"
        "## 可执行动作\n核对断言。\n## 关键证据\nT2-UNIFIED §1。\n"
        "## 验证情况\n协议样本。\n## 未知与待确认\n无。")

ALL_TYPES = ("pitfall", "workflow", "bizrule", "standard", "preference")


def test_same_input_same_routing_across_types(memory_repo):
    """相同输入：项目内+依据→全 active；无依据→全 staging。类型不改变转换。"""
    for dtype in ALL_TYPES:
        card = writer.create(memory_repo, dtype, f"{dtype} 有依据样本", BODY,
                             scope="orders", source="agent:t2:seed")
        assert not card.get("error"), (dtype, card)
        assert card["staging"] is False, (dtype, card)
        meta, _b, path = store.load(memory_repo, card["entity_id"])
        assert meta["confidence"] == "once" and meta["status"] == "active", (dtype, meta)
        assert path.parent.name != "staging", (dtype, path)
    for dtype in ALL_TYPES:
        card = writer.create(memory_repo, dtype, f"{dtype} 无依据样本 {dtype}x", BODY,
                             scope="orders")
        assert card["staging"] is True, (dtype, card)


def test_global_scope_requires_authority_regardless_of_type(memory_repo):
    """权威门只看共享范围：global 下任意类型同样 fail（无 authority_ref）。"""
    conn = index.connect(memory_repo)
    apply_all(conn)
    for dtype in ("pitfall", "standard", "bizrule"):
        card = writer.create(memory_repo, dtype, f"{dtype} 全局待审卡", BODY,
                             scope="global", source="agent:t2:seed")
        assert card["staging"] is True, (dtype, card)
        meta, body, _ = store.load(memory_repo, card["entity_id"])
        res = admission.evaluate_admission(conn, memory_repo, card["entity_id"],
                                           _compute_revision(meta, body))
        checks = {c["check"]: c["result"] for c in res["checks"]}
        assert checks["authority_confirmation"] == "fail", (dtype, res)
        assert res["result"] == "fail" and res["risk"] == "R3", (dtype, res)
    # 项目内同类卡：不要求权威，直发路径。
    card_p = writer.create(memory_repo, "pitfall", "项目内卡", BODY, scope="orders",
                           source="agent:t2:seed")
    assert card_p["staging"] is False, card_p
    meta, body, _ = store.load(memory_repo, card_p["entity_id"])
    res = admission.evaluate_admission(conn, memory_repo, card_p["entity_id"],
                                       _compute_revision(meta, body))
    checks = {c["check"]: c["result"] for c in res["checks"]}
    assert checks["authority_confirmation"] == "pass", res
    conn.close()


def test_missing_provenance_is_unknown_not_type_fail(memory_repo):
    """provenance 缺失 → unknown（保持待审），不再按类型 fail。"""
    conn = index.connect(memory_repo)
    apply_all(conn)
    for dtype in ("bizrule", "standard", "pitfall"):
        card = writer.create(memory_repo, dtype, f"{dtype} 无出处卡 {dtype}y", BODY,
                             scope="orders")
        assert card["staging"] is True, (dtype, card)  # 无任何依据 → 待审
        meta, body, _ = store.load(memory_repo, card["entity_id"])
        res = admission.evaluate_admission(conn, memory_repo, card["entity_id"],
                                           _compute_revision(meta, body))
        checks = {c["check"]: c["result"] for c in res["checks"]}
        assert checks["provenance"] == "unknown", (dtype, res)
        assert checks["authority_confirmation"] != "fail", (dtype, res)
        assert res["result"] != "fail", (dtype, res)  # 不再按类型硬拒
    conn.close()


def test_validation_channel_open_to_all_types(memory_repo):
    """staged 卡绑定 read 验证通道：非 GOVERNED 类型同样受理。"""
    from knowbase import versioned_feedback
    apply_all(index.connect(memory_repo))
    card = writer.create(memory_repo, "workflow", "全局流程卡", BODY, scope="global",
                         provenance="T2 §1")
    assert card["staging"] is True, card
    mid = card["entity_id"]
    session = "s-t2v"
    json.loads(context_impl(session_id=session, event_id="lookup", scope="global",
                            current_goal="流程", host_profile="t2"))
    text = read_impl(mid, session_id=session, event_id="read-1",
                     context_event_id="lookup", host_profile="t2")
    assert "read_use_id=" in text
    conn = index.connect(memory_repo)
    try:
        use_id = conn.execute("SELECT use_id FROM knowledge_uses WHERE session_id=?",
                              (session,)).fetchone()[0]
    finally:
        conn.close()
    meta, body, _ = store.load(memory_repo, mid)
    result = versioned_feedback.apply(memory_repo, index.connect(memory_repo), mid,
                                      "helpful", "tool-t2",
                                      expected_content_revision=_compute_revision(meta, body),
                                      use_id=use_id, task_outcome_id="t2-out-1",
                                      task_result="completed", request_id="t2-fb-1")
    assert not result.get("error"), result
    assert result.get("unknown_reason") != "VALIDATION_RESULT_REQUIRED", result


def test_governance_fields_reserved_for_all_types(memory_repo):
    """治理字段防线：active pitfall（非 GOVERNED）同样拒绝直改。"""
    card = writer.create(memory_repo, "pitfall", "防线样本", BODY, scope="orders",
                         source="agent:t2:seed")
    assert card["staging"] is False
    result = writer.update(memory_repo, card["entity_id"], confidence="verified")
    assert result.get("error") == "GOVERNANCE_FIELDS_RESERVED", result


def test_revise_cli_accepts_any_published_type(memory_repo, tmp_path):
    """revise CLI 不再限定 standard/preference/bizrule。"""
    card = writer.create(memory_repo, "workflow", "可修订流程", BODY, scope="orders",
                         source="agent:t2:seed")
    mid = card["entity_id"]
    body_file = tmp_path / "new.md"
    body_file.write_text(BODY + "\n修订补充。", encoding="utf-8")
    assert cli.main(["revise", mid, "--body-file", str(body_file)]) == 0
    meta, body, _ = store.load(memory_repo, mid)
    assert "修订补充" in body and meta["confidence"] == "once"


def test_conflict_revision_routes_by_same_risk_rule():
    """修订风险与类型无关：CORRECT 类项目内=R2、global=R3；同输入同风险。"""
    assert admission.classify_risk("orders", "CORRECT") == "R2"
    assert admission.classify_risk("global", "CORRECT") == "R3"
    assert admission.classify_risk("orders", "NEW", has_basis=True) == "R1"
    assert admission.classify_risk("orders", "NEW", has_basis=False) == "R2"
    assert admission.classify_risk("global", "NEW") == "R3"
    for dtype in ("pitfall", "bizrule", "standard"):
        assert admission.classify_risk("global", "NEW") == "R3"
        assert admission.classify_risk("orders", "NEW", has_basis=True) == "R1"
