"""Phase 3 regression tests: instance-chain attribution (3-A), machine
acceptance gates (3-B), create-intent expected-location recovery (3-C).

No human step anywhere in these paths: attribution defaults to the
instance enroll chain, acceptance is judged by scripts/reports only,
and create recovery locates the landed file instead of failing for
manual review.
"""

import json
import sqlite3

import pytest

from knowbase import commit_adapter, index, store
from knowbase.acceptance import (
    check_benefit_control,
    check_cross_member,
    check_derived_eval,
    check_full_chain,
    render_markdown,
    run_all,
)
from knowbase.benefit_control import load_report, load_tasks, run_control, write_report
from knowbase.value import aggregate, events as vevents

TEAM = "t"


# ---------------------------------------------------------------- 3-A helpers

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


def used_payload(mid, rev, task_id, state="adopted"):
    return {"memory_id": mid, "content_revision": rev, "context_event_id": "ctx-1",
            "use_id": f"use-{task_id}", "state": state, "task_id": task_id, "scope": "global"}


def completed_payload(task_id):
    return {"task_id": task_id, "task_outcome_id": f"out-{task_id}",
            "result": "completed", "verification_source": "agent_report",
            "use_id": f"use-{task_id}"}


def delivered_payload(mid, rev, task_id):
    return {"delivery_id": f"del-{task_id}", "memory_id": mid, "content_revision": rev,
            "task_id": task_id, "search_id": f"search-{task_id}",
            "context_event_id": "ctx-1", "scope": "global"}


def created_payload(mid, rev):
    return {"memory_id": mid, "content_revision": rev, "scope": "global", "dtype": "pitfall"}


def enroll_row(agg_conn, inst, binding, label=""):
    agg_conn.execute(
        "INSERT INTO installations(installation_id,member_id,member_label,evidence,"
        "member_binding_id,first_seen,last_seen) VALUES(?,?,?, 'device_confirmed',?,?,?)",
        (inst, None, label, binding, "2026-01-01T00:00:00", "2026-01-01T00:00:00"))
    agg_conn.commit()


def test_first_enroll_wins_on_reimport(tmp_path):
    """3-A:同一安装实例重复导入不覆盖首次 enroll 绑定。"""
    from knowbase.value import exchange
    agg = aggregate.connect(tmp_path / "agg.db")
    pkg = {"schema": exchange.PACKAGE_SCHEMA, "package_kind": exchange.PACKAGE_KIND,
           "package_id": "PKG-first", "exported_at": "2026-01-01T00:00:00",
           "source": {"installation_id": "INST-aaa", "member_binding_id": "MBR-first",
                      "member_label": "first", "member_evidence": "device_confirmed"},
           "events": []}
    p1 = tmp_path / "p1.json"
    p1.write_text(json.dumps(pkg))
    assert exchange.import_package(agg, p1, team_id=TEAM)["counts"]["accepted"] == 0
    pkg2 = json.loads(json.dumps(pkg))
    pkg2["package_id"] = "PKG-second"
    pkg2["source"] = {"installation_id": "INST-aaa", "member_binding_id": "MBR-second",
                      "member_label": "second", "member_evidence": "device_confirmed"}
    p2 = tmp_path / "p2.json"
    p2.write_text(json.dumps(pkg2))
    exchange.import_package(agg, p2, team_id=TEAM)
    row = agg.execute("SELECT member_binding_id,member_label,first_seen FROM installations "
                      "WHERE installation_id='INST-aaa'").fetchone()
    assert row[0] == "MBR-first" and row[1] == "first"
    agg.close()


def test_default_attribution_without_manual_confirm(tmp_path):
    """3-A:无人确认下绑定归属照常计入覆盖率；分母为有归属实例数。"""
    agg = aggregate.connect(tmp_path / "agg.db")
    enroll_row(agg, "INST-a", "MBR-a")
    agg_insert(agg, event_type="knowledge.used", inst="INST-a", binding="MBR-a",
               task_id="t1", session="s1", payload=used_payload("P-aaaa00000001", "r1", "t1"))
    agg_insert(agg, event_type="knowledge.used", inst="INST-b", binding="MBR-b",
               task_id="t2", session="s2", payload=used_payload("P-aaaa00000002", "r1", "t2"))
    snap = aggregate.snapshot(agg, team_id=TEAM)
    assert snap["members"]["coverage"] == 1.0
    assert snap["members"]["instances_attributed"] == 2
    assert snap["members"]["unknown_instances"] == []
    agg.close()


def test_unknown_instances_excluded_and_listed(tmp_path):
    """3-A 负向：无归属实例不计入分母且单列；host 标签不当成员。"""
    agg = aggregate.connect(tmp_path / "agg.db")
    enroll_row(agg, "INST-a", "MBR-a")
    agg_insert(agg, event_type="knowledge.used", inst="INST-a", binding="MBR-a",
               task_id="t1", session="s1", payload=used_payload("P-aaaa00000001", "r1", "t1"))
    # host 取一个像成员名的标签，但无绑定、无映射：只能进 unknown
    agg_insert(agg, event_type="knowledge.used", inst="INST-ghost", binding="",
               task_id="t9", session="s9", payload=used_payload("P-aaaa00000009", "r1", "t9"))
    snap = aggregate.snapshot(agg, team_id=TEAM)
    assert snap["members"]["instances_attributed"] == 1
    assert snap["members"]["coverage"] == 1.0
    assert snap["members"]["effective_consumer_instances"] == 1
    unknown = snap["members"]["unknown_instances"]
    assert len(unknown) == 1 and unknown[0]["installation_id"] == "INST-ghost"
    assert snap["members"]["observed"] == 1
    agg.close()


def test_multi_install_same_binding_merges_but_counts_instances(tmp_path):
    """3-A：同绑定多实例归并为一个成员键，但分母按实例数计。"""
    agg = aggregate.connect(tmp_path / "agg.db")
    for inst in ("INST-a1", "INST-a2"):
        enroll_row(agg, inst, "MBR-a")
        agg_insert(agg, event_type="knowledge.used", inst=inst, binding="MBR-a",
                   task_id=f"t-{inst}", session=f"s-{inst}",
                   payload=used_payload("P-aaaa00000001", "r1", f"t-{inst}"))
    snap = aggregate.snapshot(agg, team_id=TEAM)
    assert snap["members"]["observed"] == 1
    assert snap["members"]["instances_attributed"] == 2
    assert snap["members"]["coverage"] == 1.0
    agg.close()


def test_confirm_member_is_audited_correction(tmp_path):
    """3-A：纠错入口留痕（旧归属、新归属、操作人、时间），且映射优先于绑定。"""
    agg = aggregate.connect(tmp_path / "agg.db")
    enroll_row(agg, "INST-a", "MBR-a")
    result = aggregate.confirm_member(agg, "INST-a", "M-9", actor="tester", note="wrong mapping")
    assert result["old_member_id"] == "" and result["member_id"] == "M-9"
    row = agg.execute("SELECT old_member_id,new_member_id,actor,note,corrected_at "
                      "FROM attribution_corrections").fetchone()
    assert row[0] == "" and row[1] == "M-9" and row[2] == "tester"
    assert row[3] == "wrong mapping" and row[4]
    attr = aggregate.attribution(agg, TEAM)
    assert attr["mapping"]["INST-a"] == "M-9"
    snap = aggregate.snapshot(agg, team_id=TEAM)
    assert snap["members"]["attribution_corrections_total"] == 1
    agg.close()


# ---------------------------------------------------------------- 3-B helpers

def full_chain_session(agg_conn, inst, binding, task_id, session, mid):
    agg_insert(agg_conn, event_type="knowledge.delivered", inst=inst, binding=binding,
               task_id=task_id, session=session, payload=delivered_payload(mid, "r1", task_id))
    agg_insert(agg_conn, event_type="knowledge.used", inst=inst, binding=binding,
               task_id=task_id, session=session, payload=used_payload(mid, "r1", task_id))
    agg_insert(agg_conn, event_type="task.completed", inst=inst, binding=binding,
               task_id=task_id, session=session, payload=completed_payload(task_id))


def test_full_chain_pass_and_failures(tmp_path):
    """3-B-2：3 会话/2 实例全链路通过；会话不足、单实例、无归属均不通过。"""
    agg = aggregate.connect(tmp_path / "agg.db")
    full_chain_session(agg, "INST-a", "MBR-a", "t1", "s1", "P-aaaa00000001")
    full_chain_session(agg, "INST-a", "MBR-a", "t2", "s2", "P-aaaa00000002")
    assert check_full_chain(agg, TEAM)["passed"] is False  # 仅 2 会话
    full_chain_session(agg, "INST-b", "MBR-b", "t3", "s3", "P-aaaa00000003")
    assert check_full_chain(agg, TEAM)["passed"] is True
    agg.close()

    agg2 = aggregate.connect(tmp_path / "agg2.db")
    full_chain_session(agg2, "INST-only", "MBR-o", "t1", "s1", "P-aaaa00000001")
    full_chain_session(agg2, "INST-only", "MBR-o", "t2", "s2", "P-aaaa00000002")
    full_chain_session(agg2, "INST-only", "MBR-o", "t3", "s3", "P-aaaa00000003")
    assert check_full_chain(agg2, TEAM)["passed"] is False  # 3 会话但仅 1 实例
    agg2.close()

    agg3 = aggregate.connect(tmp_path / "agg3.db")
    full_chain_session(agg3, "INST-ghost", "", "t1", "s1", "P-aaaa00000001")
    full_chain_session(agg3, "INST-ghost", "", "t2", "s2", "P-aaaa00000002")
    full_chain_session(agg3, "INST-ghost", "", "t3", "s3", "P-aaaa00000003")
    assert check_full_chain(agg3, TEAM)["passed"] is False  # 无归属不计数
    agg3.close()


def cross_setup(agg_conn, chain=True):
    # A 建卡 1，B 采纳并完成任务；B 建卡 2，C 采纳并完成任务；A 的真实任务：
    # 链情形采纳卡 2（形成 B→A 边，但 A→B→C 仍成立）；星形采纳自己的卡 1
    # （不产生新边，于是只有 A→B、A→C，无长度为 2 的链）。
    agg_insert(agg_conn, event_type="knowledge.created", inst="INST-a", binding="MBR-a",
               task_id="", session="", payload=created_payload("P-aaaa00000001", "r1"))
    agg_insert(agg_conn, event_type="knowledge.created", inst="INST-b", binding="MBR-b",
               task_id="", session="", payload=created_payload("P-aaaa00000002", "r1"))
    full_chain_session(agg_conn, "INST-b", "MBR-b", "tb", "sb", "P-aaaa00000001")
    full_chain_session(agg_conn, "INST-c", "MBR-c", "tc", "sc",
                       "P-aaaa00000002" if chain else "P-aaaa00000001")
    full_chain_session(agg_conn, "INST-a", "MBR-a", "ta", "sa",
                       "P-aaaa00000002" if chain else "P-aaaa00000001")


def test_cross_member_chain_pass_and_star_fail(tmp_path):
    """3-B-3：A→B→C 链通过；星形（A→B、A→C，无 B→C）不通过。"""
    agg = aggregate.connect(tmp_path / "agg.db")
    cross_setup(agg, chain=True)
    result = check_cross_member(agg, TEAM)
    assert result["passed"] is True and result["details"]["path"] == ["binding:MBR-a", "binding:MBR-b", "binding:MBR-c"]
    agg.close()

    agg2 = aggregate.connect(tmp_path / "agg2.db")
    cross_setup(agg2, chain=False)
    assert check_cross_member(agg2, TEAM)["passed"] is False
    agg2.close()


def stub_executor(task, use_library):
    if use_library:
        return {"input_tokens": 100, "output_tokens": 50, "seconds": 2.0}
    return {"input_tokens": 500, "output_tokens": 200, "seconds": 5.0}


def test_benefit_control_pass_and_tamper_fail(tmp_path):
    """3-B-4：配对对照产出真实净数；篡改净数/摘要均不通过；缺失不通过。"""
    tasks = [{"task_id": "k1", "query": "q1"}, {"task_id": "k2", "query": "q2"}]
    report = run_control(tasks, stub_executor)
    assert report["totals"]["net_token_savings"] == 2 * (700 - 150)
    path = tmp_path / "control.json"
    write_report(report, path)
    assert check_benefit_control(path)["passed"] is True

    tampered = json.loads(path.read_text())
    tampered["totals"]["net_token_savings"] = 99999
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(tampered))
    assert check_benefit_control(bad)["passed"] is False  # 摘要不符

    retampered = json.loads(path.read_text())
    retampered["totals"]["net_token_savings"] = 99999
    from knowbase.benefit_control import _digest
    retampered["digest"] = _digest({"pairs": retampered["pairs"],
                                    "totals": retampered["totals"]})
    bad2 = tmp_path / "bad2.json"
    bad2.write_text(json.dumps(retampered))
    assert check_benefit_control(bad2)["passed"] is False  # 重算不符

    none_report = json.loads(path.read_text())
    none_report["totals"]["net_token_savings"] = None
    none_report["digest"] = _digest({"pairs": none_report["pairs"],
                                     "totals": none_report["totals"]})
    bad3 = tmp_path / "bad3.json"
    bad3.write_text(json.dumps(none_report))
    assert check_benefit_control(bad3)["passed"] is False  # None 永不充数
    assert check_benefit_control(None)["passed"] is False
    assert check_benefit_control(tmp_path / "missing.json")["passed"] is False


def test_derived_eval_pass_and_drift_fail(memory_repo, tmp_path):
    """3-B-1：派生报告 PASS 且回测一致通过；证据翻转致漂移不通过。"""
    from test_phase2_eval_gates import seed_use_evidence
    from knowbase.execution_eval import derive_dataset, write_dataset
    from knowbase.retrieval_evaluation import freeze_report

    repo = memory_repo
    conn = index.connect(repo)
    try:
        created = commit_adapter.adapter_create(
            repo, conn, "pitfall", "派生验收卡",
            "## 结论\n内容\n## 解决的问题\n问题", scope="test-proj")
        mid = created["entity_id"]
    finally:
        conn.close()
    seed_use_evidence(repo, mid, "派生查询词", feedback="helpful", outcome="completed")
    conn = index.connect(repo)
    rows, stats = derive_dataset(conn)
    assert len(rows) == 1 and rows[0]["labels"]["sample_class"] == "positive"
    dataset_path = tmp_path / "dataset.jsonl"
    digest_info = write_dataset(rows, dataset_path)
    comparison = {"metrics": {"B": {"Recall@5": 0.5, "correct_rejection": 0.5},
                              "D": {"Recall@5": 0.8, "correct_rejection": 0.8,
                                    "negative_queries": 1}},
                  "paired_intervals": {"D-B": {"CI95": [0.0, 0.2], "delta": 0.3,
                                               "positive_n": 1,
                                               "method": "paired_query_bootstrap"}},
                  "p95_lexical_compute_ms": 100}
    report = freeze_report(comparison, dataset_sha256=digest_info["sha256"],
                           vectors_sha256="v", provider_fp="fp", space_id="space",
                           covered_scopes=["global"], deployment_id="d",
                           dense_min=0, derived_stats=stats)
    assert report["verdict"] == "PASS"
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(report))
    assert check_derived_eval(conn, report_path, dataset_path)["passed"] is True

    # 证据翻转：正反馈改负向 → 重派生类别变化 → 漂移不通过
    conn.execute("UPDATE feedback_events SET outcome='not_helpful' WHERE outcome='helpful'")
    conn.commit()
    assert check_derived_eval(conn, report_path, dataset_path)["passed"] is False
    conn.close()


def test_full_acceptance_report_has_no_pending_review_residue(memory_repo, tmp_path):
    """3-B 验收：四项全过时报告 verdict PASS，且全文无待复核类残留。"""
    from test_phase2_eval_gates import seed_use_evidence
    from knowbase.execution_eval import derive_dataset, write_dataset
    from knowbase.retrieval_evaluation import freeze_report

    repo = memory_repo
    conn = index.connect(repo)
    try:
        created = commit_adapter.adapter_create(
            repo, conn, "pitfall", "验收全过卡",
            "## 结论\n内容\n## 解决的问题\n问题", scope="test-proj")
        mid = created["entity_id"]
    finally:
        conn.close()
    seed_use_evidence(repo, mid, "验收查询词", feedback="helpful", outcome="completed")
    conn = index.connect(repo)
    rows, stats = derive_dataset(conn)
    dataset_path = tmp_path / "dataset.jsonl"
    digest_info = write_dataset(rows, dataset_path)
    comparison = {"metrics": {"B": {"Recall@5": 0.5, "correct_rejection": 0.5},
                              "D": {"Recall@5": 0.8, "correct_rejection": 0.8,
                                    "negative_queries": 1}},
                  "paired_intervals": {"D-B": {"CI95": [0.0, 0.2], "delta": 0.3,
                                               "positive_n": 1,
                                               "method": "paired_query_bootstrap"}},
                  "p95_lexical_compute_ms": 100}
    report = freeze_report(comparison, dataset_sha256=digest_info["sha256"],
                           vectors_sha256="v", provider_fp="fp", space_id="space",
                           covered_scopes=["global"], deployment_id="d",
                           dense_min=0, derived_stats=stats)
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(report))

    agg = aggregate.connect(tmp_path / "agg.db")
    cross_setup(agg, chain=True)
    control_path = tmp_path / "control.json"
    write_report(run_control([{"task_id": "k1", "query": "q1"}], stub_executor), control_path)
    result = run_all(agg_conn=agg, team_id=TEAM, personal_conn=conn,
                     eval_report=report_path, dataset=dataset_path,
                     control_report=control_path)
    assert result["verdict"] == "PASS"
    text = render_markdown(result) + json.dumps(result, ensure_ascii=False)
    for residue in ("待使用者复核", "待复核", "needs_human", "NEEDS_HUMAN",
                    "pending review", "manual review"):
        assert residue not in text
    conn.close()
    agg.close()


def test_acceptance_cli_pass_and_fail(tmp_path):
    """3-B CLI：acceptance-check 全过 exit 0，缺对照报告 exit 2。"""
    from knowbase.__main__ import main
    agg = aggregate.connect(tmp_path / "agg.db")
    cross_setup(agg, chain=True)
    agg.close()
    control_path = tmp_path / "control.json"
    write_report(run_control([{"task_id": "k1", "query": "q1"}], stub_executor), control_path)
    out = tmp_path / "accept.md"
    # 缺派生输入时 check 1 不通过 → 整体 FAIL → exit 2（机器判定，非跳过）
    assert main(["acceptance-check", "--agg-db", str(tmp_path / "agg.db"),
                 "--team-id", "t",
                 "--control-report", str(control_path), "--out", str(out)]) == 2
    assert out.exists()


def test_benefit_control_cli(tmp_path):
    """3-B CLI：benefit-control 以 module:function 执行器跑对照并落盘。"""
    from knowbase.__main__ import main
    mod = tmp_path / "stub_exec.py"
    mod.write_text(
        "def run(task, use_library):\n"
        "    return {'input_tokens': 10, 'output_tokens': 5, 'seconds': 1.0} \\\n"
        "        if use_library else {'input_tokens': 50, 'output_tokens': 20, 'seconds': 3.0}\n")
    import sys
    sys.path.insert(0, str(tmp_path))
    try:
        tasks = tmp_path / "tasks.json"
        tasks.write_text(json.dumps([{"task_id": "k1", "query": "q1"}]))
        out = tmp_path / "control.json"
        assert main(["benefit-control", "--tasks", str(tasks),
                     "--executor", "stub_exec:run", "--out", str(out)]) == 0
        loaded = load_report(out)
        assert loaded["totals"]["net_token_savings"] == 70 - 15
    finally:
        sys.path.remove(str(tmp_path))


# ---------------------------------------------------------------- 3-C helpers

def _insert_pending_create(conn, intent_id, request_id, phash, mid, path):
    now = "2026-01-01T00:00:00"
    conn.execute(
        "INSERT INTO write_intents(intent_id,request_id,entity_id,scope,kind,"
        "payload_hash,status,created_at,expected_entity_id,expected_path)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (intent_id, request_id, "", "test-proj", "create", phash, "pending", now,
         mid, str(path)))
    conn.commit()


def _card_file(repo, mid, body):
    meta = store.new_meta("pitfall", "恢复定位卡", "test-proj", [], "agent:test")
    meta["id"] = mid
    path = store.mem_path(repo, "pitfall", mid)
    path.write_text(store.render(meta, body), encoding="utf-8")
    return meta, body, path


def test_create_persists_expected_location(memory_repo):
    """3-C：正常 create 即持久化期望 entity_id/路径（完成行上可查）。"""
    repo = memory_repo
    conn = index.connect(repo)
    try:
        created = commit_adapter.adapter_create(
            repo, conn, "pitfall", "期望定位卡",
            "## 结论\n内容\n## 解决的问题\n问题", scope="test-proj")
        row = conn.execute("SELECT expected_entity_id,expected_path FROM write_intents "
                           "WHERE entity_id=?", (created["entity_id"],)).fetchone()
        assert row[0] == created["entity_id"]
        assert row[1] == created["path"]
        biz = commit_adapter.adapter_create(
            repo, conn, "bizrule", "结算红线规则",
            "## 结论\n规则内容\n## 解决的问题\n问题", scope="test-proj")
        row2 = conn.execute("SELECT expected_path FROM write_intents WHERE entity_id=?",
                            (biz["entity_id"],)).fetchone()
        assert "/staging/" in row2[0]
    finally:
        conn.close()


def test_create_recovery_landed_without_heads(memory_repo):
    """3-C：崩溃在写盘后、heads 入账前 → 按期望定位并修复 heads 后恢复。"""
    repo = memory_repo
    conn = index.connect(repo)
    try:
        mid = "P-abcdef000001"
        meta, body, path = _card_file(repo, mid, "## 结论\n已落盘\n## 解决的问题\n问题")
        phash = commit_adapter._payload_hash({"type": "pitfall", "title": "t"})
        _insert_pending_create(conn, "INT-land000001", "REQ-LAND", phash, mid, path)
        out = commit_adapter.recover_intents(repo, conn)
        assert out["recovered"] == 1 and out["failed"] == 0
        row = conn.execute("SELECT status FROM write_intents WHERE intent_id='INT-land000001'").fetchone()
        assert row[0] == "completed"
        from knowbase.mutations import _compute_revision
        head = conn.execute("SELECT content_revision FROM entity_heads WHERE entity_id=?",
                            (mid,)).fetchone()
        assert head[0] == _compute_revision(meta, body)
    finally:
        conn.close()


def test_create_recovery_unlanded_and_legacy_stay_pending(memory_repo):
    """3-C 负向：定位不到（有期望无文件、 legacy 无期望）均保留 pending，
    绝不标 failed、绝无人工核查残留。"""
    repo = memory_repo
    conn = index.connect(repo)
    try:
        phash = commit_adapter._payload_hash({"type": "pitfall", "title": "t"})
        _insert_pending_create(conn, "INT-nofile00001", "REQ-NOFILE", phash,
                               "P-abcdef000002", repo / "pitfall" / "P-abcdef000002.md")
        conn.execute(
            "INSERT INTO write_intents(intent_id,request_id,entity_id,scope,kind,"
            "payload_hash,status,created_at) VALUES(?,?,?,?,?,?,?,?)",
            ("INT-legacy00001", "REQ-LEGACY", "", "test-proj", "create", phash,
             "pending", "2026-01-01T00:00:00"))
        conn.commit()
        out = commit_adapter.recover_intents(repo, conn)
        assert out["recovered"] == 0 and out["failed"] == 0
        statuses = dict(conn.execute("SELECT intent_id,status FROM write_intents "
                                     "WHERE intent_id LIKE 'INT-%'").fetchall())
        assert statuses["INT-nofile00001"] == "pending"
        assert statuses["INT-legacy00001"] == "pending"
        blob = json.dumps(out, ensure_ascii=False)
        assert "manual" not in blob and "人工" not in blob
    finally:
        conn.close()


def test_create_recovery_sibling_and_mismatch(memory_repo):
    """3-C：同 request 已由重试完成则收账；期望文件内容 id 对不上不猜测。"""
    repo = memory_repo
    conn = index.connect(repo)
    try:
        phash = commit_adapter._payload_hash({"type": "pitfall", "title": "t"})
        conn.execute(
            "INSERT INTO write_intents(intent_id,request_id,entity_id,scope,kind,"
            "payload_hash,status,created_at,expected_entity_id,expected_path)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("INT-done0000001", "REQ-SIB", "P-abcdef000003", "test-proj", "create",
             phash, "completed", "2026-01-01T00:00:00", "P-abcdef000003", ""))
        conn.commit()
        _insert_pending_create(conn, "INT-sib00000001", "REQ-SIB", phash,
                               "P-abcdef000009", repo / "pitfall" / "P-abcdef000009.md")
        out = commit_adapter.recover_intents(repo, conn)
        assert out["recovered"] == 1
        row = conn.execute("SELECT status FROM write_intents WHERE intent_id='INT-sib00000001'").fetchone()
        assert row[0] == "completed"

        # 内容 id 与期望不符：保留 pending，不把别人的卡算成自己的创建结果
        mid = "P-abcdef000004"
        meta = store.new_meta("pitfall", "错位卡", "test-proj", [], "agent:test")
        meta["id"] = "P-abcdef000005"
        path = store.mem_path(repo, "pitfall", mid)
        path.write_text(store.render(meta, "## 结论\n错位\n## 解决的问题\n问题"),
                        encoding="utf-8")
        _insert_pending_create(conn, "INT-mis00000001", "REQ-MIS", phash, mid, path)
        out = commit_adapter.recover_intents(repo, conn)
        assert out["failed"] == 0
        row = conn.execute("SELECT status FROM write_intents WHERE intent_id='INT-mis00000001'").fetchone()
        assert row[0] == "pending"
    finally:
        conn.close()


def test_create_retry_after_pending_really_executes(memory_repo):
    """3-C：pending 不短路重试；重试成功后恢复按 sibling 规则收账旧 intent。"""
    repo = memory_repo
    conn = index.connect(repo)
    try:
        phash = commit_adapter._payload_hash(
            {"type": "pitfall", "title": "重试创建卡", "body": "## 结论\n内容\n## 解决的问题\n问题",
             "scope": "test-proj", "tags": [], "source": ""})
        _insert_pending_create(conn, "INT-retry000001", "REQ-RETRY", phash,
                               "P-abcdef000006", repo / "pitfall" / "P-abcdef000006.md")
        retried = commit_adapter.adapter_create(
            repo, conn, "pitfall", "重试创建卡",
            "## 结论\n内容\n## 解决的问题\n问题", scope="test-proj", request_id="REQ-RETRY")
        assert retried.get("created") is True
        out = commit_adapter.recover_intents(repo, conn)
        row = conn.execute("SELECT status FROM write_intents WHERE intent_id='INT-retry000001'").fetchone()
        assert row[0] == "completed"
        assert out["failed"] == 0
    finally:
        conn.close()
