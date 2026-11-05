"""V2-02 运行健康与积压可解释恢复：attempt/effective-success/blocked 三分离、
结构化维护告警、只读 retry-plan、active.json 实测安装摘要。"""
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from knowbase import config, index, migrations
from knowbase.automation import worker


def test_migration_v30_adds_health_columns(memory_repo):
    conn = index.connect(memory_repo)
    try:
        applied = migrations.apply_all(conn)
        assert 30 in applied or conn.execute(
            "SELECT 1 FROM schema_migrations WHERE version=30").fetchone()
        cols = {r[1] for r in conn.execute("PRAGMA table_info(automation_schedule)")}
        assert {"last_attempt_at", "last_effective_success_at", "blocked_since",
                "build_loaded", "result_counts"} <= cols
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "maintenance_alerts" in tables
    finally:
        conn.close()


def _prepare(repo):
    conn = index.connect(memory_repo := repo)
    migrations.apply_all(conn)
    lease = worker.acquire_lease(conn, "test-owner")
    assert lease, "lease acquisition failed"
    worker.ensure_schedule(conn)
    return conn, lease


def test_reschedule_separates_attempt_effective_success_and_records_build(memory_repo):
    conn, lease = _prepare(memory_repo)
    try:
        assert worker._reschedule(conn, "knowledge_sync", status="success", error="", lease=lease)
        row = conn.execute("SELECT last_attempt_at,last_effective_success_at,blocked_since,"
                           "build_loaded,result_counts,last_status FROM automation_schedule "
                           "WHERE task_key='knowledge_sync'").fetchone()
        assert row[0] and row[1] and row[0] == row[1]
        assert row[2] == ""  # 未 blocked
        assert row[3]  # build_loaded 非空：实际执行进程的构建指纹
        counts = json.loads(row[4])
        assert counts.get("success") == 1
        # skipped 推进 attempt 但不推进 effective success。
        first_attempt = row[0]
        assert worker._reschedule(conn, "knowledge_sync", status="skipped", error="", lease=lease)
        row2 = conn.execute("SELECT last_attempt_at,last_effective_success_at,result_counts "
                            "FROM automation_schedule WHERE task_key='knowledge_sync'").fetchone()
        assert row2[0] >= first_attempt
        assert json.loads(row2[2]).get("success") == 1
        assert row2[1] == row[1]
        assert json.loads(row2[2]).get("skipped") == 1
    finally:
        worker.release_lease(conn, *lease)
        conn.close()


def test_blocked_two_rounds_emits_single_alert_then_clears(memory_repo):
    conn, lease = _prepare(memory_repo)
    try:
        # 第一轮 blocked：记 blocked_since，不出事件（可能是瞬时窗口）。
        worker._reschedule(conn, "distillation", status="blocked",
                           error="verified_executor_required", lease=lease)
        assert conn.execute("SELECT COUNT(*) FROM maintenance_alerts").fetchone()[0] == 0
        since = conn.execute("SELECT blocked_since FROM automation_schedule "
                             "WHERE task_key='distillation'").fetchone()[0]
        assert since
        # 第二轮 blocked：出 task_blocked 事件。
        worker._reschedule(conn, "distillation", status="blocked",
                           error="verified_executor_required", lease=lease)
        alerts = conn.execute("SELECT kind,subject FROM maintenance_alerts").fetchall()
        assert ("task_blocked", "distillation") in alerts
        # 第三轮 blocked：同一状态不重复刷屏。
        worker._reschedule(conn, "distillation", status="blocked",
                           error="verified_executor_required", lease=lease)
        assert conn.execute("SELECT COUNT(*) FROM maintenance_alerts WHERE kind='task_blocked' "
                            "AND subject='distillation'").fetchone()[0] == 1
        # 恢复成功：blocked_since 清空。
        worker._reschedule(conn, "distillation", status="success", error="", lease=lease)
        assert conn.execute("SELECT blocked_since FROM automation_schedule "
                            "WHERE task_key='distillation'").fetchone()[0] == ""
    finally:
        worker.release_lease(conn, *lease)
        conn.close()


def test_persistent_blocked_over_one_hour_alerts_once(memory_repo):
    conn, lease = _prepare(memory_repo)
    try:
        old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(timespec="seconds")
        conn.execute("UPDATE automation_schedule SET blocked_since=?,last_status='blocked' "
                     "WHERE task_key='distillation'", (old,))
        conn.commit()
        worker._reschedule(conn, "distillation", status="blocked",
                           error="verified_executor_required", lease=lease)
        assert conn.execute("SELECT COUNT(*) FROM maintenance_alerts WHERE "
                            "kind='task_blocked_persistent' AND subject='distillation'"
                            ).fetchone()[0] == 1
        # 再跑一轮不重复。
        worker._reschedule(conn, "distillation", status="blocked",
                           error="verified_executor_required", lease=lease)
        assert conn.execute("SELECT COUNT(*) FROM maintenance_alerts WHERE "
                            "kind='task_blocked_persistent' AND subject='distillation'"
                            ).fetchone()[0] == 1
    finally:
        worker.release_lease(conn, *lease)
        conn.close()


def test_retry_plan_is_readonly_and_classifies(memory_repo):
    conn, lease = _prepare(memory_repo)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    future = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(timespec="seconds")
    seeds = [
        ("J-requeue", "capture", "retry_wait", "", None),
        ("J-stuck", "capture", "claimed", "", future),
        ("J-review", "capture", "needs_review", "", None),
        ("J-failed", "capture", "failed", "timeout", None),
        ("J-blocked", "capture", "blocked", "material_manifest_required", None),
    ]
    for job_id, kind, state, error, lease_until in seeds:
        conn.execute(
            "INSERT INTO jobs(job_id,kind,scope,input_hash,idempotency_key,state,attempt,"
            "max_attempts,lease_until,fencing_token,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (job_id, kind, "orders", "x", job_id, state, 1, 3, lease_until, 0, now, now))
    conn.commit()
    before = conn.execute("SELECT job_id,state,error FROM jobs ORDER BY job_id").fetchall()
    worker.release_lease(conn, *lease)
    conn.close()

    plan = worker.retry_plan(memory_repo)
    assert plan["readonly"] is True
    classes = {cls: {e["job_id"] for e in items} for cls, items in plan["classes"].items()}
    assert classes["requeue_ready"] == {"J-requeue"}
    assert classes["lease_stuck"] == {"J-stuck"}
    assert classes["needs_review"] == {"J-review"}
    assert classes["failed_permanent"] == {"J-failed"}
    assert classes["blocked_config"] == {"J-blocked"}
    assert plan["summary"]["requeue_ready"] == 1
    # 只读：任何 job 状态/错误未变。
    conn = index.connect(memory_repo)
    after = conn.execute("SELECT job_id,state,error FROM jobs ORDER BY job_id").fetchall()
    conn.close()
    assert after == before


def test_status_reports_build_layers_and_oldest_pending(memory_repo):
    conn, lease = _prepare(memory_repo)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO jobs(job_id,kind,scope,input_hash,idempotency_key,state,attempt,"
        "max_attempts,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("J-old", "capture", "orders", "x", "J-old", "awaiting_agent", 0, 3, now, now))
    conn.commit()
    worker.release_lease(conn, *lease)
    conn.close()
    result = worker.status(memory_repo)
    sched = {s["kind"]: s for s in result["schedule"]}
    assert "distillation" in sched
    assert sched["distillation"]["oldest_pending"] is None or isinstance(
        sched["distillation"]["oldest_pending"], str)
    # build 层：loaded 指纹 + 会话分组（无会话时 current/stale 均空，不伪造）。
    assert result["build"]["loaded"]
    assert isinstance(result["build"]["sessions"], dict)
    assert isinstance(result["alerts"], list)


def test_doctor_automation_health_check(memory_repo):
    from knowbase import doctor
    conn, lease = _prepare(memory_repo)
    worker._reschedule(conn, "distillation", status="blocked",
                       error="verified_executor_required", lease=lease)
    worker._reschedule(conn, "distillation", status="blocked",
                       error="verified_executor_required", lease=lease)
    worker.release_lease(conn, *lease)
    conn.close()
    checks = {c["name"]: c for c in doctor.inspect(memory_repo)}
    health = checks["automation_health"]
    assert health["level"] == "warn"
    payload = json.loads(health["message"])
    assert any(b["kind"] == "distillation" for b in payload["blocked"])
    assert payload["alerts_total"] >= 1
    assert "distillation" in payload["loaded_builds"]
