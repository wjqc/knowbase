"""P0 证据修复（无人值守方案 §4）测试：检索→递送→采用身份链与验收口径。

覆盖：v25 三张表、context.execute 只为最终候选写递送、采用回执的
递送关联（有递送链接 / 无递送保留未关联）、knowledge.delivered 事件、
任务结果证据、派生类别修正（negative 不推 unanswerable、无采用为
unknown、显式 irrelevant、旧数据 legacy_unlinked）、验收门 full_chain
独立递送要求与 cross_member 成员归并、benefit 对照元数据与背景成本。
"""

import json
import re

import pytest

from knowbase import config, context, delivery, execution_eval, index
from knowbase import consumption, versioned_feedback
from knowbase.acceptance import (check_benefit_control, check_cross_member,
                                 check_full_chain)
from knowbase.benefit_control import run_control, write_report
from knowbase.retrieval_evaluation import validate_labels
from knowbase.server import save_impl
from knowbase.value import aggregate
from knowbase.value import events as vevents

CARD = """## 结论
已结算订单不能撤回；状态校验在服务层执行。

## 解决的问题
阻止已结算订单从旧页面被重复撤回。

## 适用条件
订单已结算，且当前用户请求撤回订单。

## 不适用条件
草稿订单和退单流程有单独规则。

## 可执行动作
读取订单状态，在保存前拒绝已结算订单的撤回请求。

## 关键证据
PRD-ORDER 第四条定义了结算后撤回限制。

## 验证情况
在测试项目复现已结算订单的撤回请求，服务层拒绝，数据库状态保持已结算。

## 未知与待确认
跨系统退单流程尚未验证。
"""


def seed_card(repo, title="已结算订单撤回的状态校验", dtype="pitfall"):
    out = save_impl(dtype, title, CARD, scope="orders",
                    provenance="PRD-ORDER §4" if dtype == "bizrule" else None)
    match = re.search(r"已保存 ([PB]-[\w-]+)", out)
    assert match, out
    return match.group(1)


def run_execute(repo, event="e1", queries=None, **extra):
    return context.execute(repo, session_id="s1", event_id=event, scope="orders",
                           stage="subtask_start", task_id="t1", task_revision="task-v1",
                           current_goal="订单撤回校验",
                           topics=[{"topic_id": "p1",
                                    "queries": queries or ["已结算 订单 撤回"],
                                    "status": "active"}],
                           **extra)


def test_migration_v25_tables_exist(memory_repo):
    conn = index.connect(memory_repo)
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"retrieval_deliveries", "use_retrieval_links", "task_evidence"} <= tables
    version = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
    assert version >= 25
    conn.close()


def test_execute_records_delivery_only_for_final_candidates(memory_repo):
    mid = seed_card(memory_repo)
    seed_card(memory_repo, title=" staging 规则卡片", dtype="bizrule")
    result = run_execute(memory_repo)
    assert result["candidates"] and result["candidates"][0]["id"] == mid
    assert result["delivery_ids"]
    conn = index.connect(memory_repo)
    rows = conn.execute(
        "SELECT delivery_id,search_id,context_event_id,task_id,scope,memory_id,"
        "content_revision,rank FROM retrieval_deliveries").fetchall()
    # 只有最终候选写递送：staging 的规则卡被 _candidate 过滤，不应有递送行。
    assert len(rows) == len(result["candidates"])
    assert rows[0][5] == mid and rows[0][2] == "e1" and rows[0][3] == "t1"
    assert rows[0][4] == "orders" and rows[0][1] and rows[0][7] == 1
    assert rows[0][0] == result["delivery_ids"][0]
    # 同一事件重放不重复写递送（幂等回执路径）。
    assert run_execute(memory_repo) == result
    assert conn.execute("SELECT COUNT(*) FROM retrieval_deliveries").fetchone()[0] == len(rows)
    conn.close()


def test_use_record_links_to_delivery_and_legacy_stays_unlinked(memory_repo):
    mid = seed_card(memory_repo)
    result = run_execute(memory_repo)
    cand = result["candidates"][0]
    linked = consumption.record(memory_repo, session_id="s1", event_id="u1",
                                context_event_id="e1", memory_id=mid,
                                content_revision=cand["content_revision"], state="adopted")
    assert linked["delivery_linked"] is True and linked["delivery_id"]
    conn = index.connect(memory_repo)
    assert conn.execute(
        "SELECT COUNT(*) FROM use_retrieval_links WHERE use_id=?",
        (linked["use_id"],)).fetchone()[0] == 1
    conn.close()

    # 模拟旧数据：递送行缺失时回执仍成功，但保留未关联、不猜测。
    # 注意 e2 须用不同查询，否则覆盖缓存会直接跳过检索、无新候选。
    result2 = run_execute(memory_repo, event="e2", queries=["订单 撤回"])
    cand2 = result2["candidates"][0]
    conn = index.connect(memory_repo)
    conn.execute("DELETE FROM retrieval_deliveries")
    conn.commit()
    conn.close()
    legacy = consumption.record(memory_repo, session_id="s1", event_id="u2",
                                context_event_id="e2", memory_id=mid,
                                content_revision=cand2["content_revision"], state="adopted")
    assert legacy["delivery_linked"] is False and legacy["delivery_id"] is None
    conn = index.connect(memory_repo)
    assert conn.execute(
        "SELECT COUNT(*) FROM use_retrieval_links WHERE use_id=?",
        (legacy["use_id"],)).fetchone()[0] == 0
    conn.close()


def test_knowledge_delivered_event_emitted_when_enabled(memory_repo, tmp_path):
    cfg_path = tmp_path / "knowbase.json"
    cfg = json.loads(config.CONFIG_PATH.read_text())
    cfg["value"]["events_enabled"] = True
    cfg_path.write_text(json.dumps(cfg))
    import knowbase.config as _cfg_mod
    old = _cfg_mod.CONFIG_PATH
    _cfg_mod.CONFIG_PATH = cfg_path
    try:
        seed_card(memory_repo)
        result = run_execute(memory_repo)
        assert result["delivery_ids"]
        conn = index.connect(memory_repo)
        rows = conn.execute(
            "SELECT event_type,payload_json FROM value_outbox WHERE event_type='knowledge.delivered'"
        ).fetchall()
        assert len(rows) == len(result["delivery_ids"])
        payload = json.loads(rows[0][1])["payload"]
        assert payload["delivery_id"] == result["delivery_ids"][0]
        assert payload["task_id"] == "t1" and payload["memory_id"]
        conn.close()
    finally:
        _cfg_mod.CONFIG_PATH = old


def test_task_evidence_written_on_bound_feedback(memory_repo):
    mid = seed_card(memory_repo)
    result = run_execute(memory_repo)
    cand = result["candidates"][0]
    use = consumption.record(memory_repo, session_id="s1", event_id="u1",
                             context_event_id="e1", memory_id=mid,
                             content_revision=cand["content_revision"], state="adopted")
    conn = index.connect(memory_repo)
    applied = versioned_feedback.apply(
        memory_repo, conn, mid, "helpful", "pytest",
        expected_content_revision=cand["content_revision"], use_id=use["use_id"],
        task_outcome_id="out-1", task_result="completed")
    assert not applied.get("error"), applied
    row = conn.execute(
        "SELECT task_outcome_id,use_id,memory_id,content_revision,kind,source,result,"
        "locator FROM task_evidence WHERE use_id=?", (use["use_id"],)).fetchone()
    assert row is not None
    assert row[0] == "out-1" and row[2] == mid and row[4] == "task_outcome"
    assert row[5] == "agent_report" and row[6] == "completed" and row[7] == "out-1"
    conn.close()


# ---------- 派生类别修正 ----------

def seed_derived(repo, search_id, mid, rev, *, use_state=None, feedback=None,
                 outcome=None, with_delivery=True):
    """按正式身份链播种一次 search：usage_log + delivery + link + use + 证据。"""
    from knowbase.embedding_provider import digest
    conn = index.connect(repo)
    conn.execute(
        "INSERT INTO usage_log VALUES(?,?,?,?,?)",
        ("2026-01-01T00:00:00", "pytest", "search", "",
         json.dumps({"search_id": search_id, "query": "HAR 查询词", "scope": "global",
                     "hits": [{"id": mid, "title": "seeded", "rank": 1, "score": 1.0,
                               "confidence": "verified", "status": "active"}]},
                    ensure_ascii=False)))
    if with_delivery:
        did = delivery.record_delivery(
            conn, search_id=search_id, host_profile="pytest", session_id="sess",
            context_event_id="ctx-" + search_id, task_id="task-1", task_revision="rev-1",
            scope="global", memory_id=mid, content_revision=rev, rank=1)
    if use_state is not None:
        use_id = "use-" + search_id
        conn.execute("INSERT INTO knowledge_uses VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (use_id, "pytest", "sess", "evt-" + search_id, "hash",
                      "ctx-" + search_id, "task-1", "rev-1", "global", mid, rev,
                      use_state, 1, "2026-01-01T00:00:00"))
        if with_delivery:
            delivery.record_use_link(conn, use_id=use_id, delivery_id=did,
                                     search_id=search_id)
        if outcome is not None:
            conn.execute("INSERT INTO task_outcomes VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                         ("out-" + search_id, "task-1", "run-1", outcome, "agent_report",
                          "pytest", "sess", use_id, mid, "2026-01-01T00:00:00",
                          "2026-01-01T00:00:00"))
        if feedback is not None:
            conn.execute("INSERT INTO feedback_events VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         ("rc-" + search_id, "", "hash", mid, rev, use_id, "task-1",
                          "out-" + search_id, "pytest", feedback, "version_task", "applied",
                          "b", "a", "{}", "{}", "2026-01-01T00:00:00"))
    conn.commit()
    conn.close()




def test_derive_classes_positive_negative_unknown_irrelevant_legacy(memory_repo):
    mid = seed_card(memory_repo)
    conn = index.connect(memory_repo)
    rev = conn.execute("SELECT content_revision FROM entity_heads WHERE entity_id=?",
                       (mid,)).fetchone()[0]
    conn.close()
    seed_derived(memory_repo, "s-pos", mid, rev, use_state="adopted",
                 feedback="helpful", outcome="completed")
    seed_derived(memory_repo, "s-neg", mid, rev, use_state="adopted",
                 feedback="not_helpful", outcome="failed")
    seed_derived(memory_repo, "s-unk", mid, rev)
    seed_derived(memory_repo, "s-irr", mid, rev, use_state="irrelevant")
    seed_derived(memory_repo, "s-leg", mid, rev, with_delivery=False)
    seed_derived(memory_repo, "s-unres", mid, rev, use_state="adopted",
                 outcome="completed", feedback=None)
    conn = index.connect(memory_repo)
    rows, stats = execution_eval.derive_dataset(conn)
    conn.close()
    by_id = {r["id"]: r for r in rows}
    assert by_id["exec-s-pos"]["labels"]["sample_class"] == "positive"
    assert by_id["exec-s-pos"]["labels"]["relevant_ids"] == [mid]
    assert by_id["exec-s-pos"]["labels"]["unanswerable"] is False
    neg = by_id["exec-s-neg"]["labels"]
    assert neg["sample_class"] == "negative" and neg["relevant_ids"] == []
    assert neg["unanswerable"] is False  # 单卡失败不推出查询不可回答
    unk = by_id["exec-s-unk"]["labels"]
    assert unk["sample_class"] == "unknown" and unk["unanswerable"] is False
    assert by_id["exec-s-unk"]["evidence"]["linkage"] == "linked"
    assert by_id["exec-s-irr"]["labels"]["sample_class"] == "irrelevant"
    leg = by_id["exec-s-leg"]
    assert leg["labels"]["sample_class"] == "unknown"
    assert leg["evidence"]["linkage"] == "legacy_unlinked"
    assert "exec-s-unres" not in by_id  # 采用无结论仍跳过、不补类
    assert stats["derived"] == 5 and stats["positive"] == 1 and stats["negative"] == 1
    assert stats["unknown"] == 2 and stats["irrelevant"] == 1
    assert stats["skipped_unresolved"] == 1
    assert not validate_labels(rows)


def test_formal_path_derives_positive(memory_repo):
    """正式入口全链：execute → record → feedback → derive 为 positive。"""
    mid = seed_card(memory_repo)
    result = run_execute(memory_repo)
    cand = result["candidates"][0]
    use = consumption.record(memory_repo, session_id="s1", event_id="u1",
                             context_event_id="e1", memory_id=mid,
                             content_revision=cand["content_revision"], state="adopted")
    conn = index.connect(memory_repo)
    applied = versioned_feedback.apply(
        memory_repo, conn, mid, "helpful", "pytest",
        expected_content_revision=cand["content_revision"], use_id=use["use_id"],
        task_outcome_id="out-1", task_result="completed")
    assert not applied.get("error"), applied
    rows, stats = execution_eval.derive_dataset(conn)
    conn.close()
    assert len(rows) == 1 and rows[0]["labels"]["sample_class"] == "positive"
    assert rows[0]["evidence"]["linkage"] == "linked"
    assert stats["positive"] == 1


# ---------- 验收门 ----------

TEAM = "team-test"


def agg_insert(agg_conn, *, event_type, inst, binding, task_id, session, payload,
               occurred="2026-01-01T00:00:00"):
    ev = vevents.build_event(event_type, payload, host="h1", session_id=session,
                             task_id=task_id, installation_id=inst,
                             member_binding_id=binding, occurred_at=occurred)
    agg_conn.execute(
        "INSERT INTO agg_events(team_id,event_id,event_type,installation_id,member_binding_id,"
        "host,task_id,occurred_at,session_id,payload_json,payload_hash,source_key,state,imported_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'accepted',?)",
        (TEAM, ev["event_id"], event_type, inst, binding, "h1", task_id, occurred, session,
         json.dumps(ev, ensure_ascii=False), vevents.payload_hash(ev), "", occurred))
    agg_conn.commit()
    return ev


def delivered_payload(mid, rev, task_id, delivery_id):
    return {"delivery_id": delivery_id, "memory_id": mid, "content_revision": rev,
            "task_id": task_id, "search_id": "search-" + task_id,
            "context_event_id": "ctx-" + task_id, "scope": "global"}


def used_payload(mid, rev, task_id, state="adopted"):
    return {"memory_id": mid, "content_revision": rev, "context_event_id": "ctx-" + task_id,
            "use_id": "use-" + task_id, "state": state, "task_id": task_id, "scope": "global"}


def completed_payload(task_id):
    return {"task_id": task_id, "task_outcome_id": "out-" + task_id,
            "result": "completed", "verification_source": "agent_report",
            "use_id": "use-" + task_id, "memory_id": "P-aaaa00000001"}


def full_chain_session(agg_conn, inst, binding, task_id, session, mid, *, delivered=True):
    if delivered:
        agg_insert(agg_conn, event_type="knowledge.delivered", inst=inst, binding=binding,
                   task_id=task_id, session=session,
                   payload=delivered_payload(mid, "r1", task_id, "del-" + task_id))
    agg_insert(agg_conn, event_type="knowledge.used", inst=inst, binding=binding,
               task_id=task_id, session=session, payload=used_payload(mid, "r1", task_id))
    agg_insert(agg_conn, event_type="task.completed", inst=inst, binding=binding,
               task_id=task_id, session=session, payload=completed_payload(task_id))


def test_full_chain_requires_independent_delivery(tmp_path):
    agg = aggregate.connect(tmp_path / "agg.db")
    full_chain_session(agg, "INST-a", "MBR-a", "t1", "s1", "P-aaaa00000001",
                       delivered=False)
    full_chain_session(agg, "INST-a", "MBR-a", "t2", "s2", "P-aaaa00000002",
                       delivered=False)
    full_chain_session(agg, "INST-b", "MBR-b", "t3", "s3", "P-aaaa00000003",
                       delivered=False)
    # 只有 adopted + completed、没有独立递送事件：不通过。
    assert check_full_chain(agg, TEAM)["passed"] is False
    agg.close()

    agg2 = aggregate.connect(tmp_path / "agg2.db")
    full_chain_session(agg2, "INST-a", "MBR-a", "t1", "s1", "P-aaaa00000001")
    full_chain_session(agg2, "INST-a", "MBR-a", "t2", "s2", "P-aaaa00000002")
    full_chain_session(agg2, "INST-b", "MBR-b", "t3", "s3", "P-aaaa00000003")
    assert check_full_chain(agg2, TEAM)["passed"] is True
    agg2.close()

    # 完成事件缺 use_id 绑定：不算该采用的全链。
    agg3 = aggregate.connect(tmp_path / "agg3.db")
    full_chain_session(agg3, "INST-a", "MBR-a", "t1", "s1", "P-aaaa00000001")
    full_chain_session(agg3, "INST-a", "MBR-a", "t2", "s2", "P-aaaa00000002")
    agg_insert(agg3, event_type="knowledge.delivered", inst="INST-b", binding="MBR-b",
               task_id="t3", session="s3",
               payload=delivered_payload("P-aaaa00000003", "r1", "t3", "del-t3"))
    agg_insert(agg3, event_type="knowledge.used", inst="INST-b", binding="MBR-b",
               task_id="t3", session="s3",
               payload=used_payload("P-aaaa00000003", "r1", "t3"))
    no_use = completed_payload("t3")
    del no_use["use_id"]
    agg_insert(agg3, event_type="task.completed", inst="INST-b", binding="MBR-b",
               task_id="t3", session="s3", payload=no_use)
    assert check_full_chain(agg3, TEAM)["passed"] is False  # 仅 2 有效会话
    agg3.close()


def cross_setup(agg_conn, *, same_member=False):
    # A 建卡 1，B 采纳并完成；B 建卡 2，C 采纳并完成；A 采纳卡 2 形成 B→A 边。
    b_inst, b_bind = ("INST-a2", "MBR-a") if same_member else ("INST-b", "MBR-b")
    c_inst, c_bind = ("INST-a3", "MBR-a") if same_member else ("INST-c", "MBR-c")
    agg_insert(agg_conn, event_type="knowledge.created", inst="INST-a", binding="MBR-a",
               task_id="", session="",
               payload={"memory_id": "P-aaaa00000001", "content_revision": "r1",
                        "scope": "global", "dtype": "pitfall"})
    agg_insert(agg_conn, event_type="knowledge.created", inst=b_inst, binding=b_bind,
               task_id="", session="",
               payload={"memory_id": "P-aaaa00000002", "content_revision": "r1",
                        "scope": "global", "dtype": "pitfall"})
    full_chain_session(agg_conn, b_inst, b_bind, "tb", "sb", "P-aaaa00000001")
    full_chain_session(agg_conn, c_inst, c_bind, "tc", "sc", "P-aaaa00000002")
    full_chain_session(agg_conn, "INST-a", "MBR-a", "ta", "sa", "P-aaaa00000002")


def test_cross_member_merges_instances_to_members(tmp_path):
    agg = aggregate.connect(tmp_path / "agg.db")
    cross_setup(agg)
    result = check_cross_member(agg, TEAM)
    assert result["passed"] is True
    assert result["details"]["path"] == ["binding:MBR-a", "binding:MBR-b", "binding:MBR-c"]
    agg.close()

    # G4 反例：同一人三设备（同一绑定）即使形成 A→B→C 形边也只算一人。
    agg2 = aggregate.connect(tmp_path / "agg2.db")
    cross_setup(agg2, same_member=True)
    assert check_cross_member(agg2, TEAM)["passed"] is False
    agg2.close()


def test_benefit_control_metadata_background_and_validation(tmp_path):
    tasks = [{"task_id": "k1", "query": "q1"}]

    def stub(task, use_library):
        return {"input_tokens": 100, "output_tokens": 50, "seconds": 2.0} \
            if use_library else {"input_tokens": 500, "output_tokens": 200, "seconds": 5.0}

    report = run_control(tasks, stub, metadata={"model": "m1", "model_version": "v1",
                                                "tool": "t", "task_budget": "b"},
                         background={"tokens": 40, "seconds": 1.5, "actual_cost": None})
    assert report["metadata"]["model"] == "m1"
    assert report["totals"]["background_tokens"] == 40
    assert report["totals"]["actual_cost"] is None
    path = tmp_path / "control.json"
    write_report(report, path)
    assert check_benefit_control(path)["passed"] is True

    bad = json.loads(path.read_text())
    bad["totals"]["actual_cost"] = "unknown"
    from knowbase.benefit_control import _digest
    bad["digest"] = _digest({"pairs": bad["pairs"], "totals": bad["totals"]})
    bad_path = tmp_path / "bad.json"
    bad_path.write_text(json.dumps(bad))
    assert check_benefit_control(bad_path)["passed"] is False  # 费用占位不充数

    with pytest.raises(ValueError):
        run_control(tasks, stub, background={"tokens": -1})


CARD2 = """## 结论
退款单据须核对退款金额与原始付款金额一致。

## 解决的问题
防止退款金额录入错误导致多退。

## 适用条件
退款申请已生成，且财务复核中。

## 不适用条件
未付款的退款走单独流程。

## 可执行动作
读取退款单据金额，与原始付款金额比对，不一致时拒绝提交。

## 关键证据
FIN-REFUND 第二条定义了退款金额核对规则。

## 验证情况
在测试项目复现退款金额不一致提交，系统拒绝，单据保持待复核。

## 未知与待确认
跨币种退款尚未验证。
"""


def test_formal_mcp_path_distinct_ids_and_positive(memory_repo):
    """§9 正式 MCP 入口：context_impl → use_impl → feedback_impl → 派生为 positive；
    两次不同 event_id 产生不同 search_id 与 delivery_id，互不串扰。"""
    import json
    from knowbase.server import context_impl, feedback_impl, use_impl
    seed_card(memory_repo)
    ctx1 = json.loads(context_impl(session_id="s1", event_id="e-mcp-1", scope="orders",
                                   task_id="t-mcp", task_revision="plan-v1",
                                   current_goal="已结算订单撤回", host_profile="pytest"))
    assert not ctx1.get("error"), ctx1
    assert ctx1["candidates"] and ctx1["delivery_ids"]
    cand = ctx1["candidates"][0]
    use = json.loads(use_impl(session_id="s1", event_id="u-mcp-1", context_event_id="e-mcp-1",
                              id=cand["id"], content_revision=cand["content_revision"],
                              state="adopted", host_profile="pytest"))
    assert not use.get("error"), use
    assert use["delivery_linked"] is True and use["delivery_id"] == ctx1["delivery_ids"][0]
    fb = feedback_impl(id=cand["id"], outcome="helpful",
                       expected_content_revision=cand["content_revision"],
                       use_id=use["use_id"], task_outcome_id="out-mcp-1",
                       task_result="completed")
    assert fb.startswith("已记录反馈"), fb
    # 注意：正式入口 topics=None 时复用上一事件 topics，current_goal 只在无 prior 时生效；
    # 第二个事件须传显式新 topic 才会产生新检索（既有行为，非 P0 缺陷）。
    ctx2 = json.loads(context_impl(session_id="s1", event_id="e-mcp-2", scope="orders",
                                   task_id="t-mcp", task_revision="plan-v1",
                                   current_goal="订单撤回状态", host_profile="pytest",
                                   topics=[{"topic_id": "p2", "queries": ["订单 撤回 状态"],
                                            "status": "active"}]))
    assert not ctx2.get("error"), ctx2
    assert ctx2["delivery_ids"] and ctx2["delivery_ids"] != ctx1["delivery_ids"]
    conn = index.connect(memory_repo)
    by_event = {r[0]: r[1] for r in conn.execute(
        "SELECT context_event_id, search_id FROM retrieval_deliveries")}
    assert by_event["e-mcp-1"] != by_event["e-mcp-2"]
    rows, stats = execution_eval.derive_dataset(conn)
    conn.close()
    by_search = {r["evidence"]["search_id"]: r for r in rows}
    assert by_search[by_event["e-mcp-1"]]["labels"]["sample_class"] == "positive"
    assert by_search[by_event["e-mcp-2"]]["labels"]["sample_class"] == "unknown"


def test_multi_query_deliveries_bound_to_own_search(memory_repo):
    """§9 多 query：同一事件内两条查询各命中不同卡，递送按各自 search_id 归属，
    不按最近时间或单个 context_event_id 猜测。"""
    mid1 = seed_card(memory_repo)
    out2 = save_impl("pitfall", "退款单据金额核对", CARD2, scope="orders")
    mid2 = re.search(r"已保存 ([PB]-[\w-]+)", out2).group(1)
    result = context.execute(memory_repo, session_id="s1", event_id="e-multi", scope="orders",
                             stage="subtask_start", task_id="t1", task_revision="task-v1",
                             current_goal="",
                             topics=[{"topic_id": "p1", "queries": ["已结算 订单 撤回"], "status": "active"},
                                     {"topic_id": "p2", "queries": ["退款 单据"], "status": "active"}])
    assert not result.get("error"), result
    ids = {c["id"] for c in result["candidates"]}
    assert {mid1, mid2} <= ids
    assert len(result["delivery_ids"]) == 2
    conn = index.connect(memory_repo)
    rows = conn.execute(
        "SELECT memory_id, search_id FROM retrieval_deliveries WHERE context_event_id='e-multi'").fetchall()
    assert len(rows) == 2
    by_card = {r[0]: r[1] for r in rows}
    assert by_card[mid1] != by_card[mid2]
    cand1 = next(c for c in result["candidates"] if c["id"] == mid1)
    use = consumption.record(memory_repo, session_id="s1", event_id="u-multi",
                             context_event_id="e-multi", memory_id=mid1,
                             content_revision=cand1["content_revision"], state="adopted")
    assert use["delivery_linked"] is True
    assert use["delivery_id"] == delivery.delivery_id_for(
        by_card[mid1], mid1, cand1["content_revision"])
    conn.close()
