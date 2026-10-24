"""P3 同步与证据测试（方案 §6、§9 P3 行）。

覆盖：supervisor 租约单主、调度到期与提炼阻断、MCP 拒领维护任务、
capture 触顶记 gap 不静默丢弃、推送 SHA 读回确认与分层诊断、
v2 包确定性与上限与白名单、v2 导入幂等与冲突隔离与旧协议门、
证据分支本地远端传输与回执 ACK（含重发同包）。
"""

import json
import subprocess
from pathlib import Path

import pytest

from knowbase import config, gitops, index, jobs
from knowbase.automation import worker as p3worker
from knowbase.value import aggregate, events_v2, exchange_v2
from knowbase.team.transport import GitData

GIT_ENV = {"GIT_AUTHOR_NAME": "p3-test", "GIT_AUTHOR_EMAIL": "p3@test.local",
           "GIT_COMMITTER_NAME": "p3-test", "GIT_COMMITTER_EMAIL": "p3@test.local"}


def _git(repo, *args, env=None):
    import os
    full = dict(os.environ)
    full.update(GIT_ENV)
    if env:
        full.update(env)
    subprocess.run(["git", "-C", str(repo), *args], check=True, env=full,
                   capture_output=True, text=True)


def _v2_event(event_id, delivery_id, **payload_extra):
    payload = {"delivery_id": delivery_id, "memory_id": "M1",
               "content_revision": "r1", "task_id": "T1"}
    payload.update(payload_extra)
    return {"event_id": event_id, "event_type": "retrieval.delivered",
            "occurred_at": "2026-10-24T10:00:00+00:00", "team_id": "default",
            "member_id": "m1", "installation_id": "inst-1",
            "schema_version": "knowbase.value.events.v1", "payload": payload}


# ---------- 租约与调度 ----------

def test_lease_single_owner_and_release_rules(memory_repo):
    conn = p3worker._connect(memory_repo)
    try:
        first = p3worker.acquire_lease(conn, "owner-a")
        assert first is not None
        assert p3worker.acquire_lease(conn, "owner-b") is None
        assert p3worker.release_lease(conn, "owner-b") is False
        assert p3worker.lease_status(conn)["held"] is True
        assert p3worker.release_lease(conn, "owner-a") is True
        assert p3worker.acquire_lease(conn, "owner-b") is not None
    finally:
        conn.close()


def test_tick_runs_due_tasks_and_distillation_stays_blocked(memory_repo):
    result = p3worker.tick(memory_repo)
    assert result["lease"] == "acquired"
    kinds = {r["kind"]: r for r in result["ran"]}
    assert "distillation" in kinds
    assert kinds["distillation"]["result"] == {"blocked": "distillation_blocked_no_executor"}
    assert "lease_recovery" in kinds and "knowledge_sync" in kinds
    status = p3worker.status(memory_repo)
    states = {row['kind']: row for row in status['schedule']}
    assert states['distillation']['last_status'] == 'blocked'
    assert states['distillation']['last_success_at'] is None
    assert states['team_sync']['last_status'] == 'skipped'
    assert states['team_sync']['last_success_at'] is None
    assert states['lease_recovery']['last_status'] == 'success'
    assert states['lease_recovery']['last_success_at']
    # 成功任务已重排到未来：第二轮 tick 无到期任务。
    second = p3worker.tick(memory_repo)
    assert second["ran"] == []


def test_tick_held_by_other_does_nothing(memory_repo):
    conn = p3worker._connect(memory_repo)
    try:
        p3worker.acquire_lease(conn, "someone-else")
    finally:
        conn.close()
    result = p3worker.tick(memory_repo)
    assert result["lease"] == "held_by_other"
    assert result["ran"] == []


# ---------- 任务分派与 gap ----------

def test_mcp_claim_refuses_maintenance_job(memory_repo):
    conn = index.connect(memory_repo)
    try:
        maint = jobs.enqueue(conn, jobs.KIND_MAINT_SYNC, "s", {"note": "x"}, maintenance=True)
        assert "job_id" in maint
        assert jobs.set_preparing(conn, maint["job_id"])
        assert jobs.set_awaiting(conn, maint["job_id"], 0)
        refused = jobs.claim(conn, maint["job_id"], "h1", kinds=jobs.PUBLIC_CLAIM_KINDS)
        assert refused["error"] == jobs.NO_WORK
        cap = jobs.enqueue(conn, jobs.KIND_CAPTURE, "s", {"note": "y"})
        assert "job_id" in cap
        assert jobs.set_preparing(conn, cap["job_id"])
        assert jobs.set_awaiting(conn, cap["job_id"], 0)
        ok = jobs.claim(conn, cap["job_id"], "h1", kinds=jobs.PUBLIC_CLAIM_KINDS)
        assert "batch_id" in ok
    finally:
        conn.close()


def test_mcp_entry_refuses_maintenance_job_by_id(memory_repo):
    from knowbase import server
    conn = index.connect(memory_repo)
    try:
        maint = jobs.enqueue(conn, jobs.KIND_MAINT_SYNC, "s", {"note": "x"}, maintenance=True)
    finally:
        conn.close()
    assert server.job_claim_impl(job_id=maint["job_id"], host_session_id="h1") == "无待领取任务"


def test_capture_backlog_records_gap_and_maintenance_bypasses(memory_repo, tmp_path, monkeypatch):
    cfg_path = tmp_path / "kb.json"
    cfg = config.load_config()
    cfg["inflight"]["max_pending_jobs"] = 0
    cfg_path.write_text(json.dumps(cfg))
    monkeypatch.setattr(config, "CONFIG_PATH", cfg_path)
    conn = index.connect(memory_repo)
    try:
        full = jobs.enqueue(conn, jobs.KIND_CAPTURE, "s", {"note": "x"})
        assert full["error"] == "BACKLOG_FULL"
        gaps = jobs.list_capture_gaps(conn)
        assert len(gaps) == 1 and gaps[0]["scope"] == "s"
        maint = jobs.enqueue(conn, jobs.KIND_MAINT_SYNC, "s", {"note": "m"}, maintenance=True)
        assert "job_id" in maint
    finally:
        conn.close()


# ---------- 推送确认与诊断 ----------

def test_diagnose_git_failure_layers():
    assert gitops.diagnose_git_failure("Could not resolve host: example") == "dns"
    assert gitops.diagnose_git_failure("Connection refused") == "tcp"
    assert gitops.diagnose_git_failure("Proxy Authentication Required") == "proxy"
    assert gitops.diagnose_git_failure("Authentication failed") == "auth"
    assert gitops.diagnose_git_failure("the remote end hung up unexpectedly") == "protocol"
    assert gitops.diagnose_git_failure("index.lock exists") == "git"
    assert gitops.diagnose_git_failure("something entirely new") == "unknown"
    # 脱敏：诊断只返回类别，不带原始文本。
    assert "example" not in gitops.diagnose_git_failure("Could not resolve host: example")


def _local_git_pair(tmp_path):
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)
    clone = tmp_path / "clone"
    subprocess.run(["git", "init", "-b", "main", str(clone)], check=True, capture_output=True)
    _git(clone, "remote", "add", "origin", str(bare))
    (clone / "a.txt").write_text("hello\n")
    _git(clone, "add", ".")
    _git(clone, "commit", "-m", "first")
    _git(clone, "push", "-u", "origin", "main")
    return clone, bare


def test_confirm_push_local_clones(tmp_path):
    clone, _bare = _local_git_pair(tmp_path)
    head = subprocess.run(["git", "-C", str(clone), "rev-parse", "HEAD"],
                          check=True, capture_output=True, text=True).stdout.strip()
    ok = gitops.confirm_push(clone, "origin", "main", head)
    assert ok["confirmed"] is True and ok["remote_rev"] == head
    # 未推送的发散提交：远端不包含 → 不确认、不谎称成功。
    (clone / "b.txt").write_text("diverged\n")
    _git(clone, "add", ".")
    _git(clone, "commit", "-m", "diverged")
    divergent = subprocess.run(["git", "-C", str(clone), "rev-parse", "HEAD"],
                               check=True, capture_output=True, text=True).stdout.strip()
    bad = gitops.confirm_push(clone, "origin", "main", divergent)
    assert bad["confirmed"] is False and bad["reason"] == "target_not_contained"
    # 不存在的远端：网络未知状态，只重查。
    unknown = gitops.confirm_push(clone, "missing-remote", "main", head)
    assert unknown["confirmed"] is False and unknown["reason"] == "network_unknown"


# ---------- v2 包与导入 ----------

def test_v2_package_deterministic_capped_and_whitelisted():
    events = [_v2_event("E1", "D1"), _v2_event("E2", "D2")]
    source = {"installation_id": "inst-1", "member_id": "m1"}
    p1 = exchange_v2.build_package(events, source=source, target_team="t1")
    p2 = exchange_v2.build_package(list(reversed(events)), source=source, target_team="t1")
    assert p1["package_id"] == p2["package_id"]
    assert p1["event_ids"] == ["E1", "E2"]
    assert p1["rejected"] == []
    # 白名单外字段：单条拒绝、整包其余事件不受影响。
    bad = _v2_event("E3", "D3", query_text="secret query", knowledge_body="secret body")
    p3 = exchange_v2.build_package([bad, _v2_event("E4", "D4")], source=source, target_team="t1")
    assert len(p3["events"]) == 1 and len(p3["rejected"]) == 1
    assert "query_text" in p3["rejected"][0]["fields"]
    # 上限：1001 条 → 整包拒绝。
    many = [_v2_event(f"E{i}", f"D{i}") for i in range(1001)]
    assert exchange_v2.build_package(many, source=source, target_team="t1")["error"] == "PACKAGE_TOO_LARGE"


def test_v2_import_idempotent_conflict_isolated_old_peer(tmp_path):
    agg_conn = aggregate.connect(str(tmp_path / "agg.db"))
    try:
        package = exchange_v2.build_package(
            [_v2_event("E1", "D1"), _v2_event("E2", "D2")],
            source={"installation_id": "inst-1"}, target_team="t1")
        first = exchange_v2.import_package(agg_conn, package, team_id="t1")
        assert first["counts"] == {"accepted": 2, "duplicate": 0, "conflict": 0, "rejected": 0}
        second = exchange_v2.import_package(agg_conn, package, team_id="t1")
        assert second["counts"]["duplicate"] == 2
        # 同 event_id、异 payload → 冲突隔离单条，不影响其他事件。
        tampered = exchange_v2.build_package(
            [_v2_event("E1", "D1-CHANGED"), _v2_event("E3", "D3")],
            source={"installation_id": "inst-1"}, target_team="t1")
        third = exchange_v2.import_package(agg_conn, tampered, team_id="t1")
        assert third["counts"]["conflict"] == 1 and third["counts"]["accepted"] == 1
        # 旧协议包：能力门拒绝，不导入、不丢弃（调用方留待）。
        v1ish = dict(package, schema="knowbase.value.events.v1")
        assert exchange_v2.import_package(agg_conn, v1ish, team_id="t1")["error"] == "PEER_SCHEMA_UNSUPPORTED"
        # 目标团队不符：拒绝。
        assert exchange_v2.import_package(agg_conn, package, team_id="other")["error"] == "TEAM_MISMATCH"
        # 一人多机按成员绑定合并：同一安装首次 enroll 为准（v1 同规则）。
        row = agg_conn.execute("SELECT installation_id FROM installations").fetchone()
        assert row[0] == "inst-1"
    finally:
        agg_conn.close()


# ---------- 证据分支端到端 ----------

def _insert_v2_outbox_row(memory_repo, event):
    conn = index.connect(memory_repo)
    try:
        from knowbase.value.events import payload_hash
        conn.execute(
            "INSERT INTO value_outbox(event_id,schema_version,event_type,source_key,payload_json,"
            "payload_hash,state,attempt,next_attempt_at,lease_owner,lease_until,last_error_code,"
            "created_at,acknowledged_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (event["event_id"], "knowbase.value.events.v1", event["event_type"], "",
             json.dumps(event, ensure_ascii=False), payload_hash(event), "pending", 0, "", "", "",
             "", event["occurred_at"], None))
        conn.commit()
    finally:
        conn.close()


def test_v2_receipt_regenerated_when_receipt_lost(memory_repo, tmp_path):
    """收据丢失场景：包已导入（imports 有记录）但分支上无回执文件时，
    聚合端重新导入（全判 duplicate）并把回执写回分支，发送端仍可 ACK。"""
    _insert_v2_outbox_row(memory_repo, _v2_event("E1", "D1"))
    bare = tmp_path / "evidence.git"
    subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)
    git = GitData(tmp_path / "evidence-work", f"file://{bare}", ["file://"])
    branch = "knowbase-value-events-v2"
    package = exchange_v2.export_events(memory_repo, states=("pending", "leased"))
    assert exchange_v2.send_package(git, package, branch=branch)["confirmed_rev"]
    agg_conn = aggregate.connect(str(tmp_path / "agg.db"))
    try:
        # 直接导入（模拟回执写回前丢失）：imports 已有记录，分支上无回执。
        direct = exchange_v2.import_package(agg_conn, package, team_id="default")
        assert direct["counts"]["accepted"] == 1
        received = exchange_v2.receive_all(git, str(tmp_path / "agg.db"),
                                           team_id="default", branch=branch)
        assert received["imported"] == 1 and received["receipts"] == 1
    finally:
        agg_conn.close()
    ack = exchange_v2.acknowledge_receipts(git, memory_repo, branch=branch)
    assert ack["acknowledged"] == 1


def test_v2_branch_roundtrip_receipt_and_resend(memory_repo, tmp_path):
    # 注：主线 outbox.append 仍按 v1 EVENT_TYPES 校验，retrieval.delivered
    # 在 P0 合入前无法经 append 入库；此处直接写行，模拟 P0 合入后的 outbox 状态。
    _insert_v2_outbox_row(memory_repo, _v2_event("E1", "D1"))
    _insert_v2_outbox_row(memory_repo, _v2_event("E2", "D2"))
    bare = tmp_path / "evidence.git"
    subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)
    remote = f"file://{bare}"
    git = GitData(tmp_path / "evidence-work", remote, ["file://"])
    branch = "knowbase-value-events-v2"
    package = exchange_v2.export_events(memory_repo, states=("pending", "leased"))
    assert len(package["events"]) == 2
    sent = exchange_v2.send_package(git, package, branch=branch)
    assert sent["events"] == 2 and sent["confirmed_rev"]
    package_id = package["package_id"]
    assert sent["sent"] == package_id
    agg_conn = aggregate.connect(str(tmp_path / "agg.db"))
    try:
        received = exchange_v2.receive_all(git, str(tmp_path / "agg.db"), team_id="default", branch=branch)
        assert received["imported"] == 1 and received["receipts"] == 1
        rows = agg_conn.execute("SELECT COUNT(*) FROM agg_events").fetchone()[0]
        assert rows == 2
    finally:
        agg_conn.close()
    ack = exchange_v2.acknowledge_receipts(git, memory_repo, branch=branch)
    assert ack["acknowledged"] == 2 and ack["mismatched"] == [] and ack["missing"] == []
    # 已 ACK 后 worker 发送端无待发送事件。
    assert exchange_v2.send_pending(memory_repo, git, branch=branch)["sent"] == 0
    # 收据丢失 → 重发同包：确定性 package_id 相同，导入端全判 duplicate，ACK 不重复。
    again = exchange_v2.send_package(git, package, branch=branch)
    assert again.get("already_present") is True
    agg_conn = aggregate.connect(str(tmp_path / "agg.db"))
    try:
        reimport = exchange_v2.import_package(agg_conn, package, team_id="default")
        assert reimport["counts"]["duplicate"] == 2
    finally:
        agg_conn.close()
