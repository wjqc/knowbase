"""P4 维护与上线测试：项目识别、治理维护、激活策略、doctor 新增检查、接入就绪。

覆盖方案 §7 与 §9 P4 行：显式映射优先、最具体嵌套胜出、同 Git 身份
不同 checkout 同 scope、相似名不自动绑定、后台候选不产生已确认绑定、
维护 run 与 plan（只读不写）、冲突复验保持 open 及年龄报告、
激活策略门禁（证据不受影响）、doctor 新增检查、onboarding 四态与
通知仅提示一次、CLI 正式入口调用。
"""

import json
import subprocess
from datetime import date, timedelta
from pathlib import Path

import pytest

from knowbase import build_identity, config, doctor, index, store, writer
from knowbase.__main__ import (cmd_init, cmd_maintenance_run, cmd_maintenance_status,
                               cmd_onboarding, cmd_scope_bind, cmd_scope_resolve)
from knowbase.maintenance import maintenance_status, plan_maintenance, reverify_conflicts
from knowbase.onboarding import pending_notices, readiness
from knowbase.scope_binding import (bind_explicit, list_bindings, record_candidate,
                                    resolve_binding)
from knowbase.sessions import mark_notified, record_session, sessions_by_agent

BODY = ("## 结论\n库存低于阈值时触发补货提醒。\n## 解决的问题\n避免缺货。\n"
        "## 适用条件\n库存量低于安全阈值。\n## 不适用条件\n预售商品。\n"
        "## 可执行动作\n发出补货提醒。\n## 关键证据\n仓库管理规范 W-9。\n"
        "## 验证情况\n使用场景测试验证。\n## 未知与待确认\n无。\n")


def make_card(repo, title, scope="orders"):
    result = writer.create(repo, "standard", title, BODY, scope=scope, provenance="PRD-4 §2")
    assert not result.get("error"), result
    return result["entity_id"]


def make_stale_card(repo, title, stale_since: str):
    """真实路径构造可归档 stale 卡：创建→激活（3 次验证出 staging）→
    outdated 反馈打成 stale→全部反馈与 stale_since 回拨到 stale_since 当天。

    回拨是必要的：激活验证本身就是成功证据（helpful/completed），不回拨
    的话 _last_evidence 会按"近 90 天有成功证据"保护该卡——这正是 sweep
    应有的行为，不是测试绕过。
    """
    from test_phase0_self_evolution import activate, validate
    mid = make_card(repo, title)
    activate(repo, mid, "p4")
    validate(repo, mid, "p4-outdated", "agent-a", outcome="outdated")
    meta, body, path = store.load(repo, mid)
    assert meta["status"] == "stale"
    meta["stale_since"] = stale_since
    path.write_text(store.render(meta, body), encoding="utf-8")
    conn = index.connect(repo)
    try:
        index.upsert(conn, meta, body, path)
        conn.execute("UPDATE feedback_events SET created_at=? WHERE memory_id=?",
                     (stale_since + "T00:00:00", mid))
        conn.commit()
    finally:
        conn.close()
    return mid


def git(*args, cwd):
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", *args], cwd=cwd, check=True, env={**__import__("os").environ, **env},
                   capture_output=True)


# ---------- 项目识别 ----------

def test_explicit_scope_map_wins_over_confirmed_binding(memory_repo, monkeypatch):
    conn = index.connect(memory_repo)
    try:
        bind_explicit(conn, "/proj", "bound-scope")
        conn.commit()
        cfg = config.load_config()
        cfg["scope_map"] = {"/proj": "mapped-scope"}
        monkeypatch.setattr(config, "load_config", lambda: cfg)
        # scope_binding 模块直接 import config 模块并调用 load_config，
        # 上述 monkeypatch 对模块属性生效。
        result = resolve_binding(conn, "/proj/sub")
        assert result["status"] == "resolved"
        assert result["scope"] == "mapped-scope"
        assert result["source"] == "explicit"
    finally:
        conn.close()


def test_most_specific_nested_binding_wins(memory_repo):
    conn = index.connect(memory_repo)
    try:
        bind_explicit(conn, "/proj", "outer")
        bind_explicit(conn, "/proj/sub", "inner")
        conn.commit()
        assert resolve_binding(conn, "/proj/sub/file.py")["scope"] == "inner"
        assert resolve_binding(conn, "/proj/other.py")["scope"] == "outer"
    finally:
        conn.close()


def test_same_git_identity_different_checkout_same_scope(memory_repo, tmp_path):
    bare = tmp_path / "bare.git"
    subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)
    c1 = tmp_path / "clone1"
    c2 = tmp_path / "clone2"
    subprocess.run(["git", "clone", str(bare), str(c1)], check=True, capture_output=True)
    subprocess.run(["git", "clone", str(bare), str(c2)], check=True, capture_output=True)
    conn = index.connect(memory_repo)
    try:
        bind_explicit(conn, str(c1), "shared-proj")
        conn.commit()
        result = resolve_binding(conn, str(c2))
        assert result["status"] == "resolved"
        assert result["scope"] == "shared-proj"
        assert result["source"] == "git_identity"
    finally:
        conn.close()


def test_similar_name_never_auto_binds(memory_repo):
    # 已有 scope 为 myproj 的卡，但路径 /tmp/.../myproj 无映射、无绑定、无 Git 身份。
    make_card(memory_repo, "myproj 相关卡", scope="myproj")
    conn = index.connect(memory_repo)
    try:
        result = resolve_binding(conn, "/tmp/somewhere/myproj")
        assert result["status"] == "scope_unresolved"
        assert result["reason"] == "no_mapping"
    finally:
        conn.close()


def test_background_candidate_creates_no_confirmed_binding(memory_repo):
    conn = index.connect(memory_repo)
    try:
        record_candidate(conn, "/proj")
        conn.commit()
        result = resolve_binding(conn, "/proj/file.py")
        assert result["status"] == "scope_unresolved"
        bindings = list_bindings(conn)
        assert len(bindings) == 1 and bindings[0]["confirmed"] == 0
    finally:
        conn.close()


def test_scope_cli_roundtrip(memory_repo, capsys):
    assert cmd_scope_bind("/proj", "cli-scope") == 0
    capsys.readouterr()
    assert cmd_scope_resolve("/proj/file.py") == 0
    out = json.loads(capsys.readouterr().out)
    assert out["scope"] == "cli-scope"


# ---------- 治理维护 ----------

def test_plan_is_readonly_and_counts_due(memory_repo):
    old = (date.today() - timedelta(days=100)).isoformat()
    mid = make_stale_card(memory_repo, "应归档卡", old)
    conn = index.connect(memory_repo)
    try:
        plan = plan_maintenance(memory_repo, conn)
        assert any(c["id"] == mid for c in plan["stale_due_for_archive"])
        meta, _, _ = store.load(memory_repo, mid)
        assert meta["status"] == "stale"  # plan 未写盘
    finally:
        conn.close()


def test_maintenance_run_archives_and_discards(memory_repo):
    old = (date.today() - timedelta(days=100)).isoformat()
    mid = make_stale_card(memory_repo, "应归档卡", old)
    conn = index.connect(memory_repo)
    try:
        conn.execute("INSERT INTO candidates(candidate_id,job_id,batch_id,candidate_key,scope,"
                     "body_json,evidence_json,decision,duplicate_of,version,request_id,created_at,"
                     "updated_at,materialization_state) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     ("cand-old", "job-1", "", "k1", "orders", "{}", "[]", "pending", None, 1,
                      "", (date.today() - timedelta(days=40)).isoformat(), "", "needs_review"))
        conn.commit()
    finally:
        conn.close()
    assert cmd_maintenance_run() == 0
    meta, _, _ = store.load(memory_repo, mid)
    assert meta["status"] == "archived"
    assert meta.get("archived_at")
    conn = index.connect(memory_repo)
    try:
        row = conn.execute("SELECT decision,materialization_state FROM candidates "
                           "WHERE candidate_id='cand-old'").fetchone()
        assert row == ("discarded", "discarded")
    finally:
        conn.close()


def test_reverify_conflict_stays_open_with_age(memory_repo):
    from knowbase import quarantine
    mid = make_card(memory_repo, "冲突卡")
    conn = index.connect(memory_repo)
    try:
        rec = quarantine.record_conflict(memory_repo, conn, mid, "r0",
                                         "# 本地版\n", "# 远端版\n", source="test")
        old = (date.today() - timedelta(days=5)).isoformat()
        conn.execute("UPDATE card_conflicts SET created_at=? WHERE conflict_id=?",
                     (old, rec["conflict_id"]))
        conn.commit()
        report = reverify_conflicts(memory_repo, conn)
        assert len(report) == 1
        assert report[0]["status"] == "open"  # 无证据打平，绝不强选赢家
        assert report[0]["age_days"] >= 5
        assert report[0]["note"]
    finally:
        conn.close()


def test_maintenance_status_cli(memory_repo, capsys):
    assert cmd_maintenance_status() == 0
    out = json.loads(capsys.readouterr().out)
    assert set(out) >= {"stale_due_for_archive", "review_candidates_expired",
                        "pending_intents", "open_conflicts"}


# ---------- 激活策略 ----------

def _seed_evidence(memory_repo, mid, rev, n_tasks=3):
    conn = index.connect(memory_repo)
    try:
        for i in range(n_tasks):
            tk = f"task-p4-{i}"
            conn.execute("INSERT INTO knowledge_uses(use_id,host_profile,session_id,event_id,"
                         "payload_hash,context_event_id,task_id,task_revision,scope,memory_id,"
                         "content_revision,state,current_version,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (f"use-p4-{i}", "h", f"s{i}", f"e{i}", "x", "lookup", tk, "", "orders",
                          mid, rev, "adopted", 1, "2026-10-24T00:00:00"))
            conn.execute("INSERT INTO task_outcomes(task_outcome_id,task_id,task_run_id,result,"
                         "verification_source,host_profile,session_id,use_id,memory_id,created_at,"
                         "updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                         (f"out-p4-{i}", tk, "", "completed", "agent_report", "h", f"s{i}",
                          f"use-p4-{i}", mid, "2026-10-24T00:00:00", "2026-10-24T00:00:00"))
            conn.execute("INSERT INTO feedback_events(receipt_id,request_id,payload_hash,memory_id,"
                         "content_revision,use_id,task_key,task_outcome_id,agent,outcome,binding,"
                         "state,before_hash,after_hash,after_meta_json,result_json,created_at)"
                         " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (f"rc-p4-{i}", "", "x", mid, rev, f"use-p4-{i}", tk, f"out-p4-{i}",
                          "agent-a", "helpful", "version_task", "applied", "", "", "{}",
                          "{}", "2026-10-24T00:00:00"))
        conn.commit()
    finally:
        conn.close()


def test_activation_policy_gates_promotion_but_not_evidence(memory_repo, monkeypatch):
    from knowbase import evolution
    from knowbase.mutations import _compute_revision
    mid = make_card(memory_repo, "策略卡", scope="orders")
    meta, body, _ = store.load(memory_repo, mid)
    meta["status"] = "active"
    rev = _compute_revision(meta, body)
    _seed_evidence(memory_repo, mid, rev)
    conn = index.connect(memory_repo)
    try:
        counts = evolution.decide(conn, mid, rev, meta, ("h", "s9"), "task-p4-new",
                                  "helpful", "completed")
        assert counts["promote"] is True
        assert counts["helpful_tasks"] >= 3
        # 收紧策略：standard 不在允许类型中 → 不激活，但证据计数不变。
        base_load = config.load_config
        patched = base_load()
        patched["evolution"] = {**patched.get("evolution", {}),
                                "activation_policy": {"allowed_types": ["bizrule"],
                                                      "allowed_scopes": None,
                                                      "policy_revision": "2"}}
        monkeypatch.setattr(config, "load_config", lambda: patched)
        counts2 = evolution.decide(conn, mid, rev, meta, ("h", "s9"), "task-p4-new",
                                   "helpful", "completed")
        assert counts2["promote"] is False
        assert counts2["helpful_tasks"] == counts["helpful_tasks"]
        # 收紧范围：scope 不在允许范围 → 同样不激活。
        patched["evolution"]["activation_policy"] = {"allowed_types": ["standard"],
                                                      "allowed_scopes": ["other-scope"],
                                                      "policy_revision": "2"}
        counts3 = evolution.decide(conn, mid, rev, meta, ("h", "s9"), "task-p4-new",
                                   "helpful", "completed")
        assert counts3["promote"] is False
    finally:
        conn.close()


# ---------- doctor 新增检查 ----------

def test_doctor_p4_checks_present(memory_repo):
    checks = {c["name"]: c for c in doctor.inspect(memory_repo)}
    for name in ("scope_bindings", "activation_policy", "maintenance", "sync_confirmed",
                 "pending_ack", "evidence_link_coverage", "worker_lease",
                 "session_states", "executor_capability"):
        assert name in checks, name
    # 合并后：P0/P1/P3 均已合入，降级分支不再触发，三项检查输出真实状态。
    # （单批 P4 分支上的旧断言是“P0 递送表不存在 / P1 未合入 / P3 未合入”
    # 的降级文案；合并树里表与模块都已存在，断言须随之改为真实输出。）
    cov = json.loads(checks["evidence_link_coverage"]["message"])
    assert cov == {"uses": 0, "linked_uses": 0, "coverage": None}
    assert checks["worker_lease"]["message"] == "无租约记录"
    caps = json.loads(checks["executor_capability"]["message"])
    assert "discovered" in caps
    ap = json.loads(checks["activation_policy"]["message"])
    assert ap["policy_revision"] == "1"
    # 默认策略不限制类型/范围（沿用已有授权范围），收紧须显式配置。
    assert ap["allowed_types"] is None
    assert ap["allowed_scopes"] is None


def test_doctor_scope_bindings_reflects_binding(memory_repo):
    conn = index.connect(memory_repo)
    try:
        bind_explicit(conn, "/proj", "doc-scope")
        conn.commit()
    finally:
        conn.close()
    checks = {c["name"]: c for c in doctor.inspect(memory_repo)}
    info = json.loads(checks["scope_bindings"]["message"])
    assert info["confirmed"] == 1


# ---------- 接入就绪与通知 ----------

def test_readiness_four_states_separate(memory_repo):
    ready = readiness(memory_repo)
    assert ready["config_complete"] is True
    assert ready["protocol_pass"] is True
    assert ready["hosts_loaded"] == []
    assert ready["real_task_verified"] is False
    assert ready["reconnect_required"] == []


def test_session_states_and_reconnect(memory_repo):
    current = build_identity.build_id()
    record_session("agent-a", str(memory_repo), current, "python3")
    record_session("agent-old", str(memory_repo), "deadbeef00000000", "python3")
    grouped = sessions_by_agent(current)
    assert "agent-a" in grouped["current"]
    assert "agent-old" in grouped["stale"]
    ready = readiness(memory_repo)
    assert "agent-a" in ready["hosts_loaded"]
    assert "agent-old" in ready["reconnect_required"]


def test_notice_only_once(memory_repo):
    first = pending_notices(memory_repo)
    assert first, "应有至少一条提示（无真实任务验证）"
    mark_notified([n["key"] for n in first])
    second = pending_notices(memory_repo)
    assert all(n["key"] not in {x["key"] for x in first} for n in second)


def test_onboarding_cli_marks_notices(memory_repo, capsys):
    assert cmd_onboarding() == 0
    out = json.loads(capsys.readouterr().out)
    assert set(out) == {"readiness", "new_notices"}
    assert cmd_onboarding() == 0
    out2 = json.loads(capsys.readouterr().out)
    assert out2["new_notices"] == []  # 第二次不再重复提示


def test_init_prints_readiness_summary(memory_repo, capsys):
    assert cmd_init() == 0
    out = capsys.readouterr().out
    assert "接入就绪" in out
    assert "配置=✓" in out
