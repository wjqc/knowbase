"""V2 任务与跨成员消费：来源账本、任务结果账本、跨成员复用指标。"""

import json

from knowbase import config, index, versioned_feedback
from knowbase.value import aggregate, events as vevents, exchange, outbox as voutbox


def enable_events():
    cfg = json.loads(config.CONFIG_PATH.read_text())
    cfg["value"] = {"events_enabled": True}
    config.CONFIG_PATH.write_text(json.dumps(cfg))


def outbox_rows(repo, event_type=None):
    conn = index.connect(repo)
    try:
        return [e for e in voutbox.list_events(conn)
                if event_type is None or e["event_type"] == event_type]
    finally:
        conn.close()


def full_card():
    return ("## 结论\n已结算订单不能撤回。\n## 解决的问题\n防重复撤回。\n## 适用条件\n订单已结算。\n"
            "## 不适用条件\n草稿订单。\n## 可执行动作\n保存前拒绝撤回。\n## 关键证据\nPRD §4。\n"
            "## 验证情况\n测试复现拒绝。\n## 未知与待确认\n无。\n")


def test_created_revised_activated_events_follow_card_lifecycle(memory_repo):
    from knowbase.__main__ import main
    from knowbase.server import save_impl, update_impl
    enable_events()
    saved = save_impl("pitfall", "订单撤回规则", full_card(), scope="orders")
    mid = json.loads(saved)["entity_id"] if saved.startswith("{") else \
        __import__("re").search(r"已保存 ([PB]-[\w-]+)", saved).group(1)
    created = outbox_rows(memory_repo, "knowledge.created")
    assert len(created) == 1 and created[0]["source_key"] == f"knowledge:{mid}:created"
    payload = json.loads(created[0]["payload"])
    assert payload["payload"]["dtype"] == "pitfall" and payload["payload"]["scope"] == "orders"

    updated = update_impl(mid, body=full_card() + "补充：跨系统退单另走流程。\n", title="订单撤回规则",
                          expected_content_revision=payload["payload"]["content_revision"])
    assert "错误" not in updated and "error" not in updated
    revised = outbox_rows(memory_repo, "knowledge.revised")
    assert len(revised) == 1
    new_rev = json.loads(revised[0]["payload"])["payload"]["content_revision"]
    assert revised[0]["source_key"] == f"knowledge:{mid}:{new_rev}:revised"

    # bizrule 走 staging → 人工 promote 产生 activated 事件（正文与 pitfall 卡不同，避免查重拦截）
    rule_body = full_card().replace("已结算订单不能撤回。", "结算完成后禁止发起撤回工单。")
    rule = save_impl("bizrule", "结算后禁止撤回工单规则", rule_body, scope="orders",
                     provenance="PRD-ORDER §5")
    rule_id = __import__("re").search(r"\b([PB]-[0-9a-f]+)\b", rule).group(1)
    assert main(["promote", rule_id]) == 0
    activated = outbox_rows(memory_repo, "knowledge.activated")
    assert len(activated) == 1 and activated[0]["source_key"] == f"knowledge:{rule_id}:activated"
    assert json.loads(activated[0]["payload"])["host"] == "cli"


def deliver_and_adopt(repo, mid, revision, *, session="s1", event="use-1"):
    from knowbase.server import context_impl, use_impl
    card = json.loads(context_impl(session_id=session, event_id="retrieval", scope="orders",
                                   task_id="t1", task_revision="v1", current_goal="订单",
                                   host_profile="h1"))["candidates"][0]
    assert card["id"] == mid
    return json.loads(use_impl(session_id=session, event_id=event, context_event_id="retrieval",
                                id=mid, content_revision=revision, state="adopted",
                                host_profile="h1"))


def test_task_outcome_ledger_and_events(memory_repo):
    enable_events()
    from knowbase.server import save_impl
    saved = save_impl("pitfall", "订单撤回规则", full_card(), scope="orders")
    import re as _re
    mid = _re.search(r"已保存 ([PB]-[\w-]+)", saved).group(1)
    conn = index.connect(memory_repo)
    revision = conn.execute("SELECT content_revision FROM entity_heads WHERE entity_id=?",
                            (mid,)).fetchone()[0]
    conn.close()
    use = deliver_and_adopt(memory_repo, mid, revision)
    conn = index.connect(memory_repo)
    result = versioned_feedback.apply(memory_repo, conn, mid, "helpful", "pytest",
                                      expected_content_revision=revision,
                                      use_id=use["use_id"], task_outcome_id="OUT-77",
                                      request_id="req-a", task_result="completed")
    conn.close()
    assert not result.get("error")
    conn = index.connect(memory_repo)
    row = conn.execute("SELECT task_id,result,verification_source FROM task_outcomes "
                       "WHERE task_outcome_id='OUT-77'").fetchone()
    conn.close()
    assert row == ("t1", "completed", "agent_report")
    events = {e["event_type"] for e in outbox_rows(memory_repo)}
    assert "task.completed" in events and "feedback.recorded" in events
    task_event = outbox_rows(memory_repo, "task.completed")[0]
    assert task_event["source_key"] == "task_outcomes:OUT-77"
    # 重放不重复
    conn = index.connect(memory_repo)
    replay = versioned_feedback.apply(memory_repo, conn, mid, "helpful", "pytest",
                                      expected_content_revision=revision,
                                      use_id=use["use_id"], task_outcome_id="OUT-77",
                                      request_id="req-a", task_result="completed")
    conn.close()
    assert replay["idempotent"] is True
    assert len(outbox_rows(memory_repo, "task.completed")) == 1
    # 非法值拒绝
    conn = index.connect(memory_repo)
    bad = versioned_feedback.apply(memory_repo, conn, mid, "helpful", "pytest",
                                   expected_content_revision=revision, use_id=use["use_id"],
                                   task_outcome_id="OUT-78", task_result="success")
    conn.close()
    assert bad["error"] == "INVALID_TASK_RESULT"
    conn = index.connect(memory_repo)
    no_id = versioned_feedback.apply(memory_repo, conn, mid, "helpful", "pytest",
                                     expected_content_revision=revision, use_id=use["use_id"],
                                     task_result="completed")
    conn.close()
    assert no_id["error"] == "TASK_RESULT_REQUIRES_OUTCOME_ID"


def _package(path, events_list, source):
    package = {"schema": exchange.PACKAGE_SCHEMA, "package_kind": exchange.PACKAGE_KIND,
               "package_id": "PKG-" + path.stem, "exported_at": "2026-10-22T00:00:00+00:00",
               "source": source, "window": {"since": "", "until": ""},
               "events": [{"event": e, "payload_hash": vevents.payload_hash(e),
                           "source_key": ""} for e in events_list]}
    path.write_text(json.dumps(package, ensure_ascii=False))
    return path


def _event(event_type, payload, installation, binding, event_id,
            occurred="2026-10-22T01:00:00+00:00", session_id="s1"):
    return vevents.build_event(event_type, payload, host="zcode", session_id=session_id,
                               installation_id=installation, member_binding_id=binding,
                               task_id=payload.get("task_id", ""), event_id=event_id,
                               occurred_at=occurred)


def test_cross_member_reuse_metrics(memory_repo, tmp_path):
    enable_events()
    from knowbase.server import save_impl
    import re as _re
    saved = save_impl("pitfall", "订单撤回规则", full_card(), scope="orders")
    mid = _re.search(r"已保存 ([PB]-[\w-]+)", saved).group(1)
    conn = index.connect(memory_repo)
    revision = conn.execute("SELECT content_revision FROM entity_heads WHERE entity_id=?",
                            (mid,)).fetchone()[0]
    conn.close()

    # 成员 A（另一台安装）创建该卡版本
    pkg_a = _package(tmp_path / "a.json",
                     [_event("knowledge.created",
                             {"memory_id": mid, "content_revision": revision,
                              "scope": "orders", "dtype": "pitfall"},
                             "INST-aaaa", "MBR-memberA", "VE-a1")],
                     {"installation_id": "INST-aaaa", "member_binding_id": "MBR-memberA",
                      "member_label": "A", "member_evidence": "manual_confirmed",
                      "member_confirmed_at": "2026-10-01T00:00:00+00:00"})
    # 成员 B 采纳该版本 + 有效反馈（任务完成）
    pkg_b = _package(tmp_path / "b.json",
                     [_event("knowledge.used",
                             {"memory_id": mid, "content_revision": revision,
                              "context_event_id": "ce", "use_id": "U1", "state": "adopted",
                              "task_id": "t-cross", "scope": "orders"},
                             "INST-bbbb", "MBR-memberB", "VE-b1"),
                      _event("feedback.recorded",
                             {"memory_id": mid, "content_revision": revision, "use_id": "U1",
                              "task_outcome_id": "OUT-9", "outcome": "helpful",
                              "binding": "version_task", "receipt_id": "FB-1",
                              "feedback_source": "agent", "task_id": "t-cross"},
                             "INST-bbbb", "MBR-memberB", "VE-b2")],
                     {"installation_id": "INST-bbbb", "member_binding_id": "MBR-memberB",
                      "member_label": "B", "member_evidence": "manual_confirmed",
                      "member_confirmed_at": "2026-10-01T00:00:00+00:00"})
    agg = aggregate.connect(tmp_path / "agg.db")
    assert exchange.import_package(agg, pkg_a, team_id="t")["counts"]["accepted"] == 1
    assert exchange.import_package(agg, pkg_b, team_id="t")["counts"]["accepted"] == 2
    snap = aggregate.snapshot(agg, team_id="t")
    cross = snap["cross_member_reuse"]
    assert cross["available"] is True
    assert cross["adopted_tasks"] == 1 and cross["cross_member_tasks"] == 1
    assert cross["unattributed_tasks"] == 0
    # agent 来源的正向反馈只算 agent_reported，不算用户确认的"明确有帮助"
    assert cross["helpful_tasks"] == 0
    assert cross["agent_reported_helpful_tasks"] == 1
    assert cross["feedback_covered_tasks"] == 1
    assert cross["feedback_coverage"] == 1.0

    # 同成员采纳不计跨成员；贡献者身份未知单列
    pkg_a2 = _package(tmp_path / "a2.json",
                      [_event("knowledge.used",
                              {"memory_id": mid, "content_revision": revision,
                               "context_event_id": "ce", "use_id": "U2", "state": "adopted",
                               "task_id": "t-same", "scope": "orders"},
                              "INST-aaaa", "MBR-memberA", "VE-a2"),
                       _event("knowledge.used",
                              {"memory_id": mid, "content_revision": revision,
                               "context_event_id": "ce", "use_id": "U3", "state": "adopted",
                               "task_id": "t-unknown", "scope": "orders"},
                              "INST-cccc", "", "VE-a3")],
                      {"installation_id": "INST-cccc", "member_binding_id": "",
                       "member_label": "", "member_evidence": "", "member_confirmed_at": ""})
    exchange.import_package(agg, pkg_a2, team_id="t")
    snap = aggregate.snapshot(agg, team_id="t")
    cross = snap["cross_member_reuse"]
    assert cross["adopted_tasks"] == 3          # 任务按 task_id 去重
    assert cross["cross_member_tasks"] == 1     # 同成员 t-same 不计；跨成员仍 1
    assert cross["unattributed_tasks"] == 1     # t-unknown 消费者未知单列
    agg.close()


def _pkg(path, events_list, source):
    return _package(path, events_list, source)


def test_helpful_verdict_source_conflict_and_task_failure(tmp_path):
    """评审复现：Agent helpful + 用户 not_helpful + 任务失败 → 不得计为有帮助。"""
    mid, rev = "P-x1", "r1"
    pkg_a = _package(tmp_path / "a.json",
                     [_event("knowledge.created",
                             {"memory_id": mid, "content_revision": rev,
                              "scope": "orders", "dtype": "pitfall"},
                             "INST-a", "MBR-A", "VE-a0", occurred="2026-09-01T00:00:00+00:00")],
                     {"installation_id": "INST-a", "member_binding_id": "MBR-A",
                      "member_label": "A", "member_evidence": "manual_confirmed",
                      "member_confirmed_at": "2026-09-01T00:00:00+00:00"})
    consumer_events = [
        _event("knowledge.used",
               {"memory_id": mid, "content_revision": rev, "context_event_id": "ce",
                "use_id": "U9", "state": "adopted", "task_id": "t-bad", "scope": "orders"},
               "INST-b", "MBR-B", "VE-b0", occurred="2026-10-10T00:00:00+00:00"),
        _event("feedback.recorded",
               {"memory_id": mid, "content_revision": rev, "use_id": "U9",
                "task_outcome_id": "OUT-1", "outcome": "helpful", "binding": "version_task",
                "receipt_id": "FB-9", "feedback_source": "agent", "task_id": "t-bad"},
               "INST-b", "MBR-B", "VE-b1", occurred="2026-10-10T00:05:00+00:00"),
        _event("feedback.recorded",
               {"memory_id": mid, "content_revision": rev, "use_id": "U9",
                "task_outcome_id": "OUT-2", "outcome": "not_helpful", "binding": "version_task",
                "receipt_id": "FB-10", "feedback_source": "user", "task_id": "t-bad"},
               "INST-b", "MBR-B", "VE-b2", occurred="2026-10-10T00:10:00+00:00"),
        _event("task.abandoned",
               {"task_id": "t-bad", "task_outcome_id": "OUT-2", "result": "abandoned",
                "verification_source": "user_confirmed", "use_id": "U9", "memory_id": mid},
               "INST-b", "MBR-B", "VE-b3", occurred="2026-10-10T00:15:00+00:00"),
    ]
    pkg_b = _package(tmp_path / "b.json", consumer_events,
                     {"installation_id": "INST-b", "member_binding_id": "MBR-B",
                      "member_label": "B", "member_evidence": "manual_confirmed",
                      "member_confirmed_at": "2026-09-01T00:00:00+00:00"})
    agg = aggregate.connect(tmp_path / "agg.db")
    exchange.import_package(agg, pkg_a, team_id="t")
    exchange.import_package(agg, pkg_b, team_id="t")
    # 展示窗口只取 10 月：上月创建的贡献者仍可归因（全量账本，不被窗口过滤）
    snap = aggregate.snapshot(agg, team_id="t", since="2026-10-01")
    cross = snap["cross_member_reuse"]
    assert cross["cross_member_tasks"] == 1 and cross["unattributed_tasks"] == 0
    assert cross["helpful_tasks"] == 0 and cross["agent_reported_helpful_tasks"] == 0
    assert cross["conflicted_tasks"] == 1
    agg.close()


def test_user_confirmed_helpful_with_completed_task_counts(tmp_path):
    mid, rev = "P-x2", "r1"
    events = [
        _event("knowledge.created",
               {"memory_id": mid, "content_revision": rev, "scope": "orders", "dtype": "pitfall"},
               "INST-a", "MBR-A", "VE-c0", occurred="2026-09-01T00:00:00+00:00"),
        _event("knowledge.used",
               {"memory_id": mid, "content_revision": rev, "context_event_id": "ce",
                "use_id": "U10", "state": "adopted", "task_id": "t-good", "scope": "orders"},
               "INST-b", "MBR-B", "VE-c1", occurred="2026-10-12T00:00:00+00:00"),
        _event("feedback.recorded",
               {"memory_id": mid, "content_revision": rev, "use_id": "U10",
                "task_outcome_id": "OUT-3", "outcome": "helpful", "binding": "version_task",
                "receipt_id": "FB-11", "feedback_source": "user", "task_id": "t-good"},
               "INST-b", "MBR-B", "VE-c2", occurred="2026-10-12T00:05:00+00:00"),
        _event("task.completed",
               {"task_id": "t-good", "task_outcome_id": "OUT-3", "result": "completed",
                "verification_source": "user_confirmed", "use_id": "U10", "memory_id": mid},
               "INST-b", "MBR-B", "VE-c3", occurred="2026-10-12T00:10:00+00:00"),
    ]
    pkg = _package(tmp_path / "good.json", events[:1],
                   {"installation_id": "INST-a", "member_binding_id": "MBR-A",
                    "member_label": "A", "member_evidence": "manual_confirmed",
                    "member_confirmed_at": "2026-09-01T00:00:00+00:00"})
    pkg2 = _package(tmp_path / "good2.json", events[1:],
                    {"installation_id": "INST-b", "member_binding_id": "MBR-B",
                     "member_label": "B", "member_evidence": "manual_confirmed",
                     "member_confirmed_at": "2026-09-01T00:00:00+00:00"})
    agg = aggregate.connect(tmp_path / "agg.db")
    exchange.import_package(agg, pkg, team_id="t")
    exchange.import_package(agg, pkg2, team_id="t")
    cross = aggregate.snapshot(agg, team_id="t")["cross_member_reuse"]
    assert cross["helpful_tasks"] == 1 and cross["agent_reported_helpful_tasks"] == 0
    assert cross["conflicted_tasks"] == 0
    agg.close()


def test_feedback_on_non_cross_cards_does_not_chain_into_verdict(tmp_path):
    """同任务内对非跨成员卡的反馈不得串联成跨成员有效性结论。"""
    cross_mid, own_mid, rev = "P-x3", "P-own", "r1"
    events_a = [
        _event("knowledge.created",
               {"memory_id": cross_mid, "content_revision": rev, "scope": "orders",
                "dtype": "pitfall"},
               "INST-a", "MBR-A", "VE-d0", occurred="2026-09-01T00:00:00+00:00"),
        _event("knowledge.created",
               {"memory_id": own_mid, "content_revision": rev, "scope": "orders",
                "dtype": "pitfall"},
               "INST-b", "MBR-B", "VE-d1", occurred="2026-10-11T00:00:00+00:00"),
    ]
    events_b = [
        _event("knowledge.used",
               {"memory_id": cross_mid, "content_revision": rev, "context_event_id": "ce",
                "use_id": "U11", "state": "adopted", "task_id": "t-mix", "scope": "orders"},
               "INST-b", "MBR-B", "VE-d2", occurred="2026-10-11T00:01:00+00:00"),
        # 只对"自己创建的那张卡"给了 helpful——不应给跨成员卡的任务结论
        _event("feedback.recorded",
               {"memory_id": own_mid, "content_revision": rev, "use_id": "U11",
                "task_outcome_id": "OUT-4", "outcome": "helpful", "binding": "version_task",
                "receipt_id": "FB-12", "feedback_source": "agent", "task_id": "t-mix"},
               "INST-b", "MBR-B", "VE-d3", occurred="2026-10-11T00:02:00+00:00"),
    ]
    agg = aggregate.connect(tmp_path / "agg.db")
    exchange.import_package(agg, _package(tmp_path / "a.json", events_a,
        {"installation_id": "INST-a", "member_binding_id": "MBR-A", "member_label": "A",
         "member_evidence": "manual_confirmed", "member_confirmed_at": "x"}), team_id="t")
    exchange.import_package(agg, _package(tmp_path / "b.json", events_b,
        {"installation_id": "INST-b", "member_binding_id": "MBR-B", "member_label": "B",
         "member_evidence": "manual_confirmed", "member_confirmed_at": "x"}), team_id="t")
    cross = aggregate.snapshot(agg, team_id="t")["cross_member_reuse"]
    assert cross["cross_member_tasks"] == 1
    assert cross["feedback_covered_tasks"] == 0
    assert cross["agent_reported_helpful_tasks"] == 0 and cross["helpful_tasks"] == 0
    agg.close()


def test_same_label_across_installations_is_one_member(tmp_path):
    """同一人多台安装确认同一标签 → 同一绑定，不算两个人/跨成员。"""
    label_binding = "MBR-sameperson"
    created = _event("knowledge.created",
                     {"memory_id": "P-x4", "content_revision": "r1", "scope": "orders",
                      "dtype": "pitfall"},
                     "INST-dev1", label_binding, "VE-e0")
    adopted = _event("knowledge.used",
                     {"memory_id": "P-x4", "content_revision": "r1", "context_event_id": "ce",
                      "use_id": "U12", "state": "adopted", "task_id": "t-self", "scope": "orders"},
                     "INST-dev2", label_binding, "VE-e1")
    agg = aggregate.connect(tmp_path / "agg.db")
    exchange.import_package(agg, _package(tmp_path / "p1.json", [created],
        {"installation_id": "INST-dev1", "member_binding_id": label_binding,
         "member_label": "qc", "member_evidence": "manual_confirmed",
         "member_confirmed_at": "x"}), team_id="t")
    exchange.import_package(agg, _package(tmp_path / "p2.json", [adopted],
        {"installation_id": "INST-dev2", "member_binding_id": label_binding,
         "member_label": "qc", "member_evidence": "manual_confirmed",
         "member_confirmed_at": "x"}), team_id="t")
    snap = aggregate.snapshot(agg, team_id="t")
    assert snap["members"]["observed"] == 1
    cross = snap["cross_member_reuse"]
    assert cross["cross_member_tasks"] == 0 and cross["unattributed_tasks"] == 0
    agg.close()


def test_old_revision_feedback_does_not_credit_new_adoption(tmp_path):
    """评审复现：采纳 r2、反馈打在 r1（另一 use_id）→ 不得计入新采纳收益。"""
    mid = "P-rev"
    events_a = [
        _event("knowledge.created",
               {"memory_id": mid, "content_revision": "r1", "scope": "orders", "dtype": "pitfall"},
               "INST-a", "MBR-A", "VE-r0", occurred="2026-10-01T00:00:00+00:00"),
        _event("knowledge.revised",
               {"memory_id": mid, "content_revision": "r2", "scope": "orders"},
               "INST-a", "MBR-A", "VE-r1", occurred="2026-10-02T00:00:00+00:00"),
    ]
    events_b = [
        _event("knowledge.used",
               {"memory_id": mid, "content_revision": "r2", "context_event_id": "ce",
                "use_id": "U-new", "state": "adopted", "task_id": "t-rev", "scope": "orders"},
               "INST-b", "MBR-B", "VE-r2", occurred="2026-10-10T00:00:00+00:00"),
        # 反馈绑定的是旧版本 r1 的另一条使用
        _event("feedback.recorded",
               {"memory_id": mid, "content_revision": "r1", "use_id": "U-old",
                "task_outcome_id": "OUT-old", "outcome": "helpful", "binding": "version_task",
                "receipt_id": "FB-old", "feedback_source": "user", "task_id": "t-rev"},
               "INST-b", "MBR-B", "VE-r3", occurred="2026-10-10T00:05:00+00:00"),
    ]
    agg = aggregate.connect(tmp_path / "agg.db")
    exchange.import_package(agg, _package(tmp_path / "a.json", events_a,
        {"installation_id": "INST-a", "member_binding_id": "MBR-A", "member_label": "A",
         "member_evidence": "manual_confirmed", "member_confirmed_at": "x"}), team_id="t")
    exchange.import_package(agg, _package(tmp_path / "b.json", events_b,
        {"installation_id": "INST-b", "member_binding_id": "MBR-B", "member_label": "B",
         "member_evidence": "manual_confirmed", "member_confirmed_at": "x"}), team_id="t")
    cross = aggregate.snapshot(agg, team_id="t")["cross_member_reuse"]
    assert cross["cross_member_tasks"] == 1  # r2 由 A 贡献、B 采纳：跨成员成立
    assert cross["helpful_tasks"] == 0 and cross["agent_reported_helpful_tasks"] == 0
    assert cross["feedback_covered_tasks"] == 0  # r1 反馈不在采纳版本上，不串联
    agg.close()


def test_same_task_id_different_members_stay_separate(tmp_path):
    """评审复现：B、C 同名任务不合并；B 的成功不被 C 的失败改写。"""
    events = [
        _event("knowledge.created",
               {"memory_id": "P-s1", "content_revision": "r1", "scope": "orders",
                "dtype": "pitfall"},
               "INST-a", "MBR-A", "VE-s0", occurred="2026-10-01T00:00:00+00:00"),
        # B：同名任务 fix-order，采纳 + 用户 helpful + completed
        _event("knowledge.used",
               {"memory_id": "P-s1", "content_revision": "r1", "context_event_id": "ce",
                "use_id": "U-b", "state": "adopted", "task_id": "fix-order", "scope": "orders"},
               "INST-b", "MBR-B", "VE-s1", occurred="2026-10-10T00:00:00+00:00"),
        _event("feedback.recorded",
               {"memory_id": "P-s1", "content_revision": "r1", "use_id": "U-b",
                "task_outcome_id": "OUT-b", "outcome": "helpful", "binding": "version_task",
                "receipt_id": "FB-b", "feedback_source": "user", "task_id": "fix-order"},
               "INST-b", "MBR-B", "VE-s2", occurred="2026-10-10T00:05:00+00:00"),
        _event("task.completed",
               {"task_id": "fix-order", "task_outcome_id": "OUT-b", "result": "completed",
                "verification_source": "user_confirmed", "use_id": "U-b", "memory_id": "P-s1"},
               "INST-b", "MBR-B", "VE-s3", occurred="2026-10-10T00:10:00+00:00"),
        # C：同名任务 fix-order，采纳另一张卡 + 任务失败
        _event("knowledge.created",
               {"memory_id": "P-s2", "content_revision": "r1", "scope": "orders",
                "dtype": "pitfall"},
               "INST-a", "MBR-A", "VE-s4", occurred="2026-10-01T00:00:00+00:00"),
        _event("knowledge.used",
               {"memory_id": "P-s2", "content_revision": "r1", "context_event_id": "ce",
                "use_id": "U-c", "state": "adopted", "task_id": "fix-order", "scope": "orders"},
               "INST-c", "MBR-C", "VE-s5", occurred="2026-10-11T00:00:00+00:00"),
        _event("task.abandoned",
               {"task_id": "fix-order", "task_outcome_id": "OUT-c", "result": "abandoned",
                "verification_source": "user_confirmed", "use_id": "U-c", "memory_id": "P-s2"},
               "INST-c", "MBR-C", "VE-s6", occurred="2026-10-11T00:10:00+00:00"),
    ]
    agg = aggregate.connect(tmp_path / "agg.db")
    exchange.import_package(agg, _package(tmp_path / "a.json", events[:1],
        {"installation_id": "INST-a", "member_binding_id": "MBR-A", "member_label": "A",
         "member_evidence": "manual_confirmed", "member_confirmed_at": "x"}), team_id="t")
    exchange.import_package(agg, _package(tmp_path / "b.json", events[1:4],
        {"installation_id": "INST-b", "member_binding_id": "MBR-B", "member_label": "B",
         "member_evidence": "manual_confirmed", "member_confirmed_at": "x"}), team_id="t")
    exchange.import_package(agg, _package(tmp_path / "c.json", [events[4], *events[5:]],
        {"installation_id": "INST-c", "member_binding_id": "MBR-C", "member_label": "C",
         "member_evidence": "manual_confirmed", "member_confirmed_at": "x"}), team_id="t")
    # C 包含 A 的第二张 created（同贡献者）
    exchange.import_package(agg, _package(tmp_path / "a2.json", [events[4]],
        {"installation_id": "INST-a", "member_binding_id": "MBR-A", "member_label": "A",
         "member_evidence": "manual_confirmed", "member_confirmed_at": "x"}), team_id="t")
    snap = aggregate.snapshot(agg, team_id="t")
    cross = snap["cross_member_reuse"]
    assert cross["adopted_tasks"] == 2      # 同名任务按归属人区分，不合并
    assert cross["cross_member_tasks"] == 2
    assert cross["helpful_tasks"] == 1      # B 的成功保留
    assert cross["conflicted_tasks"] == 0   # 不被 C 的失败改写成冲突
    assert cross["failed_excluded_tasks"] == 1  # C 单独剔除
    owners = {e["owner"] for e in snap["reuse_evidence"]}
    assert len(owners) == 2
    agg.close()


def test_coverage_numerator_is_effective_consumers(tmp_path):
    """评审复现：登记两人、仅一人创建无人消费 → coverage=0%（不是 50%）。"""
    events = [
        _event("knowledge.created",
               {"memory_id": "P-cov", "content_revision": "r1", "scope": "orders",
                "dtype": "pitfall"},
               "INST-a", "MBR-A", "VE-v0", occurred="2026-10-10T00:00:00+00:00"),
    ]
    agg = aggregate.connect(tmp_path / "agg.db")
    exchange.import_package(agg, _package(tmp_path / "a.json", events,
        {"installation_id": "INST-a", "member_binding_id": "MBR-A", "member_label": "A",
         "member_evidence": "manual_confirmed", "member_confirmed_at": "x"}), team_id="t")
    aggregate.register_member(agg, "M-001", "A")
    aggregate.register_member(agg, "M-002", "B")
    aggregate.confirm_member(agg, "INST-a", "M-001")
    snap = aggregate.snapshot(agg, team_id="t")
    members = snap["members"]
    assert members["effective_consumers"] == 0
    assert members["coverage_known"] is True
    assert members["coverage"] == 0.0  # 分子=有效消费成员，不是有事件的成员
    agg.close()


def test_feedback_with_fake_use_id_is_unlinked(tmp_path):
    """评审复现：反馈引用不存在的 use_id → 未关联单列，不得计入 helpful。"""
    mid, rev = "P-u1", "r1"
    events_a = [
        _event("knowledge.created",
               {"memory_id": mid, "content_revision": rev, "scope": "orders",
                "dtype": "pitfall"},
               "INST-a", "MBR-A", "VE-u0", occurred="2026-10-01T00:00:00+00:00"),
    ]
    events_b = [
        _event("knowledge.used",
               {"memory_id": mid, "content_revision": rev, "context_event_id": "ce",
                "use_id": "USE-REAL", "state": "adopted", "task_id": "t-u1", "scope": "orders"},
               "INST-b", "MBR-B", "VE-u1", occurred="2026-10-10T00:00:00+00:00"),
        # 反馈声称 user helpful，但 use_id 指向不存在的回执
        _event("feedback.recorded",
               {"memory_id": mid, "content_revision": rev,
                "use_id": "MISSING-CONSUMPTION-RECEIPT",
                "task_outcome_id": "OUT-x", "outcome": "helpful", "binding": "version_task",
                "receipt_id": "FB-x", "feedback_source": "user", "task_id": "t-u1"},
               "INST-b", "MBR-B", "VE-u2", occurred="2026-10-10T00:05:00+00:00"),
    ]
    agg = aggregate.connect(tmp_path / "agg.db")
    exchange.import_package(agg, _package(tmp_path / "a.json", events_a,
        {"installation_id": "INST-a", "member_binding_id": "MBR-A", "member_label": "A",
         "member_evidence": "manual_confirmed", "member_confirmed_at": "x"}), team_id="t")
    exchange.import_package(agg, _package(tmp_path / "b.json", events_b,
        {"installation_id": "INST-b", "member_binding_id": "MBR-B", "member_label": "B",
         "member_evidence": "manual_confirmed", "member_confirmed_at": "x"}), team_id="t")
    cross = aggregate.snapshot(agg, team_id="t")["cross_member_reuse"]
    assert cross["cross_member_tasks"] == 1
    assert cross["helpful_tasks"] == 0 and cross["agent_reported_helpful_tasks"] == 0
    assert cross["feedback_covered_tasks"] == 0
    assert cross["unlinked_feedback_events"] == 1  # 待关联单列，不猜
    agg.close()


def test_same_member_two_installations_same_task_stay_separate(tmp_path):
    """评审复现：同一成员 ZCode/Claude 两安装同名任务 → 两个任务运行；
    后一次失败不改写前一次的用户确认 helpful。"""
    mid, rev = "P-u2", "r1"
    common = [
        _event("knowledge.created",
               {"memory_id": mid, "content_revision": rev, "scope": "orders",
                "dtype": "pitfall"},
               "INST-a", "MBR-A", "VE-w0", occurred="2026-10-01T00:00:00+00:00"),
    ]
    run1 = [
        _event("knowledge.used",
               {"memory_id": mid, "content_revision": rev, "context_event_id": "ce",
                "use_id": "U-z1", "state": "adopted", "task_id": "fix-order", "scope": "orders"},
               "INST-zcode", "MBR-SAME", "VE-w1", occurred="2026-10-10T00:00:00+00:00",
               session_id="sess-zcode"),
        _event("feedback.recorded",
               {"memory_id": mid, "content_revision": rev, "use_id": "U-z1",
                "task_outcome_id": "OUT-z1", "outcome": "helpful", "binding": "version_task",
                "receipt_id": "FB-z1", "feedback_source": "user", "task_id": "fix-order"},
               "INST-zcode", "MBR-SAME", "VE-w2", occurred="2026-10-10T00:05:00+00:00",
               session_id="sess-zcode"),
        _event("task.completed",
               {"task_id": "fix-order", "task_outcome_id": "OUT-z1", "result": "completed",
                "verification_source": "user_confirmed", "use_id": "U-z1", "memory_id": mid},
               "INST-zcode", "MBR-SAME", "VE-w3", occurred="2026-10-10T00:10:00+00:00",
               session_id="sess-zcode"),
    ]
    run2 = [
        _event("knowledge.used",
               {"memory_id": mid, "content_revision": rev, "context_event_id": "ce",
                "use_id": "U-c1", "state": "adopted", "task_id": "fix-order", "scope": "orders"},
               "INST-claude", "MBR-SAME", "VE-w4", occurred="2026-10-11T00:00:00+00:00",
               session_id="sess-claude"),
        _event("task.abandoned",
               {"task_id": "fix-order", "task_outcome_id": "OUT-c1", "result": "abandoned",
                "verification_source": "user_confirmed", "use_id": "U-c1", "memory_id": mid},
               "INST-claude", "MBR-SAME", "VE-w5", occurred="2026-10-11T00:10:00+00:00",
               session_id="sess-claude"),
    ]
    agg = aggregate.connect(tmp_path / "agg.db")
    src = {"member_binding_id": "MBR-SAME", "member_label": "qc",
           "member_evidence": "manual_confirmed", "member_confirmed_at": "x"}
    exchange.import_package(agg, _package(tmp_path / "a.json", common,
        {**src, "installation_id": "INST-a", "member_binding_id": "MBR-A"}), team_id="t")
    exchange.import_package(agg, _package(tmp_path / "z.json", run1,
        {**src, "installation_id": "INST-zcode"}), team_id="t")
    exchange.import_package(agg, _package(tmp_path / "c.json", run2,
        {**src, "installation_id": "INST-claude"}), team_id="t")
    cross = aggregate.snapshot(agg, team_id="t")["cross_member_reuse"]
    assert cross["adopted_tasks"] == 2          # 同成员不同运行来源：两个任务
    assert cross["cross_member_tasks"] == 2
    assert cross["helpful_tasks"] == 1          # ZCode 会话的用户确认保留
    assert cross["conflicted_tasks"] == 0       # Claude 会话的失败不改写它
    assert cross["failed_excluded_tasks"] == 1
    agg.close()


def test_old_aggregate_db_upgrade_backfills_session_and_keeps_linkage(tmp_path):
    """评审复现：旧结构汇总库升级后，历史消费与新反馈不断开。"""
    import hashlib
    import sqlite3
    db = tmp_path / "old-agg.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
    CREATE TABLE agg_events(
      team_id TEXT NOT NULL, event_id TEXT NOT NULL, event_type TEXT NOT NULL,
      installation_id TEXT NOT NULL DEFAULT '', member_binding_id TEXT NOT NULL DEFAULT '',
      host TEXT NOT NULL DEFAULT '', task_id TEXT NOT NULL DEFAULT '',
      occurred_at TEXT NOT NULL DEFAULT '', payload_json TEXT NOT NULL,
      payload_hash TEXT NOT NULL, source_key TEXT NOT NULL DEFAULT '',
      state TEXT NOT NULL DEFAULT 'accepted', imported_at TEXT NOT NULL,
      PRIMARY KEY(team_id,event_id));
    """)
    conn.commit()
    conn.close()

    def old_import(events):
        c = sqlite3.connect(db)
        for event in events:
            c.execute(
                "INSERT INTO agg_events(team_id,event_id,event_type,installation_id,"
                "member_binding_id,host,task_id,occurred_at,payload_json,payload_hash,"
                "state,imported_at) VALUES('t',?,?,?,?,?,?,?,?,?,'accepted',?)",
                (event["event_id"], event["event_type"], event["installation_id"],
                 event["member_binding_id"], event.get("host", ""), event["task_id"],
                 event["occurred_at"], json.dumps(event, ensure_ascii=False),
                 vevents.payload_hash(event), event["occurred_at"]))
        c.commit()
        c.close()

    mid, rev = "P-old", "r1"
    # 旧写入：事件 JSON 内含 session_id，但旧表无该列
    old_import([
        vevents.build_event("knowledge.created",
                            {"memory_id": mid, "content_revision": rev, "scope": "orders",
                             "dtype": "pitfall"},
                            host="zcode", session_id="", installation_id="INST-a",
                            member_binding_id="MBR-A", task_id="",
                            event_id="VE-o0", occurred_at="2026-10-01T00:00:00+00:00"),
        vevents.build_event("knowledge.used",
                            {"memory_id": mid, "content_revision": rev,
                             "context_event_id": "ce", "use_id": "U-old", "state": "adopted",
                             "task_id": "t-old", "scope": "orders"},
                            host="zcode", session_id="sess-old",
                            installation_id="INST-b", member_binding_id="MBR-B",
                            task_id="t-old", event_id="VE-o1",
                            occurred_at="2026-10-10T00:00:00+00:00"),
    ])
    # 升级（connect 补列+回填）后，新导入同任务的用户反馈仍能关联
    agg = aggregate.connect(db)
    feedback_event = vevents.build_event(
        "feedback.recorded",
        {"memory_id": mid, "content_revision": rev, "use_id": "U-old",
         "task_outcome_id": "OUT-old", "outcome": "helpful", "binding": "version_task",
         "receipt_id": "FB-old", "feedback_source": "user", "task_id": "t-old"},
        host="zcode", session_id="sess-old", installation_id="INST-b",
        member_binding_id="MBR-B", task_id="t-old", event_id="VE-o2",
        occurred_at="2026-10-10T00:05:00+00:00")
    agg.execute(
        "INSERT INTO agg_events(team_id,event_id,event_type,installation_id,member_binding_id,"
        "host,task_id,occurred_at,session_id,payload_json,payload_hash,state,imported_at) "
        "VALUES('t',?,?,?,?,?,?,?,?,?,?,'accepted',?)",
        ("VE-o2", "feedback.recorded", "INST-b", "MBR-B", "zcode", "t-old",
         "2026-10-10T00:05:00+00:00", "sess-old", json.dumps(feedback_event),
         vevents.payload_hash(feedback_event), "2026-10-10T00:05:00+00:00"))
    agg.commit()
    backfilled = agg.execute("SELECT COUNT(*) FROM agg_events WHERE session_id='sess-old'").fetchone()[0]
    assert backfilled == 2  # 历史 used 已回填；新反馈原生带列
    cross = aggregate.snapshot(agg, team_id="t")["cross_member_reuse"]
    assert cross["cross_member_tasks"] == 1
    assert cross["helpful_tasks"] == 1  # 升级前后关联一致
    assert cross["unlinked_feedback_events"] == 0
    agg.close()


def test_weekly_bucket_carries_last_event_time(tmp_path):
    """周桶带周内最近事件时间：时间筛选按事件时间而非周起点。"""
    import sqlite3
    db = tmp_path / "agg.db"
    agg = aggregate.connect(db)
    event = {"event_id": "VE-w", "schema_version": "knowbase.value.events.v1",
             "event_type": "knowledge.used", "host": "zcode",
             "occurred_at": "2026-10-16T00:00:00+00:00", "session_id": "s",
             "payload": {"memory_id": "P", "content_revision": "r", "context_event_id": "c",
                         "use_id": "u", "state": "adopted", "task_id": "t", "scope": "o"}}
    agg.execute(
        "INSERT INTO agg_events(team_id,event_id,event_type,occurred_at,session_id,host,"
        "payload_json,payload_hash,imported_at) VALUES('t',?,?,?,?,?,?,?,?)",
        ("VE-w", "knowledge.used", "2026-10-16T00:00:00+00:00", "s", "zcode",
         json.dumps(event), vevents.payload_hash(event), "2026-10-16T00:00:00+00:00"))
    agg.commit()
    snap = aggregate.snapshot(agg, team_id="t")
    bucket = snap["weekly"][0]
    assert bucket["last"] == "2026-10-16T00:00:00+00:00"
    host_bucket = snap["weekly_by_host"]["zcode"][0]
    assert host_bucket["last"] == "2026-10-16T00:00:00+00:00"
    agg.close()


def test_daily_granularity_supports_filter_then_rebucket(tmp_path):
    """评审复现：同一 ISO 周内 10 天前与 6 天前两条事件，"近 7 天"只应计 1。"""
    mid, rev = "P-d1", "r1"

    def used(event_id, when):
        return vevents.build_event(
            "knowledge.used",
            {"memory_id": mid, "content_revision": rev, "context_event_id": "ce",
             "use_id": "U-" + event_id, "state": "adopted", "task_id": "t-d1",
             "scope": "orders"},
            host="zcode", session_id="s", installation_id="INST-b",
            member_binding_id="MBR-B", task_id="t-d1", event_id=event_id,
            occurred_at=when)

    # 2026-10-12（周一）与 2026-10-16 同属 W42；快照窗口不筛，两者都进周桶
    events = [
        _event("knowledge.created",
               {"memory_id": mid, "content_revision": rev, "scope": "orders",
                "dtype": "pitfall"},
               "INST-a", "MBR-A", "VE-d0", occurred="2026-10-01T00:00:00+00:00"),
        used("VE-d1", "2026-10-12T08:00:00+00:00"),
        used("VE-d2", "2026-10-16T08:00:00+00:00"),
    ]
    agg = aggregate.connect(tmp_path / "agg.db")
    exchange.import_package(agg, _package(tmp_path / "p.json", events,
        {"installation_id": "INST-b", "member_binding_id": "MBR-B", "member_label": "B",
         "member_evidence": "manual_confirmed", "member_confirmed_at": "x"}), team_id="t")
    snap = aggregate.snapshot(agg, team_id="t")
    agg.close()
    # 周桶确实是整周 2 条（这是它天生的粒度）
    w42 = [w for w in snap["weekly"] if w["week"].endswith("W42")]
    assert w42 and w42[0]["events"] == 2
    # 日粒度把两条事件分开，供"先过滤后重聚"（created 事件也计入其日期）
    daily = {d["date"]: d["events"] for d in snap["daily"]}
    assert daily["2026-10-12"] == 1 and daily["2026-10-16"] == 1
    assert daily.get("2026-10-01") == 1
    assert snap["daily_by_host"]["zcode"][0]["date"] in daily
    # 页面同款算法：以 2026-10-22T12:00Z 为 now、近 7 天（按当天 00:00 判定）
    # → 只剩 10-16，重聚后 W42=1，10-12 不再计入整周
    from knowbase.value.aggregate import _week_key
    from datetime import datetime, timezone
    now = int(datetime(2026, 10, 22, 12, tzinfo=timezone.utc).timestamp())
    cutoff = now - 7 * 86400
    kept = [d for d in snap["daily"]
            if datetime.fromisoformat(d["date"] + "T00:00:00+00:00").timestamp() >= cutoff]
    weeks = {}
    for d in kept:
        key = _week_key(d["date"] + "T00:00:00+00:00")
        weeks[f"{key[0]}-W{key[1]:02d}"] = weeks.get(f"{key[0]}-W{key[1]:02d}", 0) + d["events"]
    assert weeks.get("2026-W42") == 1
    assert "2026-W40" not in weeks  # 10-01（W40）同样被排除
