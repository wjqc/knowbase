"""T0 旧积压一次性撤销（jobs retire-backlog）测试。

覆盖：
- 已失败知识类任务：补 retired 回执、删 spool、保留 jobs 行；
- 未终态旧任务：结算为 no_candidate；持有效租约的在途写入不动；
- needs_review 任务与候选、成卡回执候选一律不动；
- 终态 spool 正文按保留期清理，期内不动；
- 默认只读盘点不改库；--execute 幂等，二次执行不再删除。
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from knowbase import jobs
from knowbase.__main__ import cmd_init


@pytest.fixture()
def isolated_repo(tmp_path, monkeypatch):
    from knowbase import config
    repo = tmp_path / "repo"
    cfg_path = tmp_path / "config.json"
    cfg = config._deep_merge(config.DEFAULTS, {
        "repo_path": str(repo),
        "knowledge_path": "",
        "git": {"auto_commit": False, "auto_push": False, "auto_pull": False},
    })
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", cfg_path)
    monkeypatch.setenv("KNOWBASE_REPO_PATH", str(repo))
    monkeypatch.setenv("KNOWBASE_AGENT_NAME", "pytest-agent")
    assert cmd_init() == 0
    return repo


def _old(ts: str, days: int) -> str:
    dt = datetime.fromisoformat(ts) - timedelta(days=days)
    return dt.isoformat(timespec="seconds")


def _mk_job(conn, kind, *, state="queued", days_old=0, lease_until=None, result=None):
    import uuid as _uuid
    r = jobs.enqueue(conn, kind, "knowbase",
                     {"session_id": "s-" + _uuid.uuid4().hex[:8], "project": "/p"})
    job_id = r["job_id"]
    if days_old:
        ts = conn.execute("SELECT created_at FROM jobs WHERE job_id=?", (job_id,)).fetchone()[0]
        old = _old(ts, days_old)
        conn.execute("UPDATE jobs SET created_at=?, updated_at=? WHERE job_id=?",
                     (old, old, job_id))
        conn.execute("UPDATE job_spool SET created_at=? WHERE job_id=?", (old, job_id))
    if state != "queued" or lease_until is not None or result is not None:
        conn.execute(
            "UPDATE jobs SET state=?, lease_until=?, result=? WHERE job_id=?",
            (state, lease_until, json.dumps(result) if result else None, job_id))
    conn.commit()
    return job_id


def test_failed_checkpoint_gets_receipt_and_spool_removed(isolated_repo):
    conn = jobs.connect(isolated_repo)
    try:
        failed = _mk_job(conn, jobs.KIND_CHECKPOINT, state="failed", days_old=30,
                         result={"error": "MATERIAL_PARSE_FAILED"})
        receipt = jobs.retire_backlog(conn, cutoff="2026-11-01T00:00:00+00:00")
        assert {"job_id": failed, "state": "failed"} in receipt["jobs"]
        row = conn.execute(
            "SELECT state, result FROM jobs WHERE job_id=?", (failed,)).fetchone()
        assert row[0] == "failed"
        assert json.loads(row[1])["retired"]["from_state"] == "failed"
        assert conn.execute(
            "SELECT COUNT(*) FROM job_spool WHERE job_id=?", (failed,)).fetchone()[0] == 0
    finally:
        conn.close()


def test_old_unterminal_job_settled_but_live_lease_untouched(isolated_repo):
    conn = jobs.connect(isolated_repo)
    try:
        stale = _mk_job(conn, jobs.KIND_CAPTURE, state="awaiting_agent", days_old=30)
        live = _mk_job(conn, jobs.KIND_CAPTURE, state="claimed",
                       lease_until="2999-01-01T00:00:00+00:00", days_old=30)
        receipt = jobs.retire_backlog(conn, cutoff="2026-11-01T00:00:00+00:00")
        ids = [j["job_id"] for j in receipt["jobs"]]
        assert stale in ids and live not in ids
        assert conn.execute(
            "SELECT state FROM jobs WHERE job_id=?", (stale,)).fetchone()[0] == "no_candidate"
        assert conn.execute(
            "SELECT state FROM jobs WHERE job_id=?", (live,)).fetchone()[0] == "claimed"
        # 在途任务的 spool 保留
        assert conn.execute(
            "SELECT COUNT(*) FROM job_spool WHERE job_id=?", (live,)).fetchone()[0] == 1
    finally:
        conn.close()


def test_retire_unmaterialized_review_preserves_card_receipts(isolated_repo):
    conn = jobs.connect(isolated_repo)
    try:
        review = _mk_job(conn, jobs.KIND_CAPTURE, state="needs_review", days_old=30)
        conn.execute(
            "INSERT INTO candidates(candidate_id, job_id, candidate_key, scope, body_json,"
            " evidence_json, decision, created_at, updated_at,materialization_state,payload_json)"
            " VALUES('C-x', ?, 'k1', 'knowbase', '{}', '[]', 'needs_review',"
            " '2026-01-01', '2026-01-01','needs_review','{\"body\":\"old\"}')", (review,))
        saved = _mk_job(conn, jobs.KIND_CAPTURE, state="succeeded", days_old=30)
        conn.execute(
            "INSERT INTO candidates(candidate_id, job_id, candidate_key, scope, body_json,"
            " evidence_json, decision, created_at, updated_at)"
            " VALUES('C-y', ?, 'k2', 'knowbase', '{}', '[]', 'saved',"
            " '2026-01-01', '2026-01-01')", (saved,))
        conn.commit()
        receipt = jobs.retire_backlog(conn, cutoff="2026-11-01T00:00:00+00:00")
        assert {j["job_id"] for j in receipt["jobs"]} == {review}
        assert conn.execute("SELECT materialization_state,payload_json FROM candidates WHERE candidate_id='C-x'").fetchone() == ("discarded", "{}")
        kept = conn.execute(
            "SELECT COUNT(*) FROM candidates WHERE job_id IN (?, ?)",
            (review, saved)).fetchone()[0]
        assert kept == 2
        assert conn.execute(
            "SELECT job_id FROM job_spool WHERE job_id IN (?,?)", (review,saved)).fetchall() == [(saved,)]
    finally:
        conn.close()


def test_stale_terminal_spool_pruned_recent_kept(isolated_repo):
    conn = jobs.connect(isolated_repo)
    try:
        old_done = _mk_job(conn, jobs.KIND_CAPTURE, state="succeeded", days_old=30)
        new_done = _mk_job(conn, jobs.KIND_CAPTURE, state="succeeded", days_old=1)
        receipt = jobs.retire_backlog(conn, cutoff="2026-11-01T00:00:00+00:00",
                                      spool_days=7)
        assert receipt["stale_spool_deleted"] >= 1
        assert conn.execute(
            "SELECT COUNT(*) FROM job_spool WHERE job_id=?", (old_done,)).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM job_spool WHERE job_id=?", (new_done,)).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE job_id IN (?, ?)",
            (old_done, new_done)).fetchone()[0] == 2
    finally:
        conn.close()


def test_dry_run_plan_is_readonly_and_idempotent_execute(isolated_repo):
    conn = jobs.connect(isolated_repo)
    try:
        failed = _mk_job(conn, jobs.KIND_CHECKPOINT, state="failed", days_old=30)
        before = conn.execute("SELECT state, result FROM jobs WHERE job_id=?",
                              (failed,)).fetchone()
        plan = jobs.retire_plan(conn, cutoff="2026-11-01T00:00:00+00:00")
        assert len(plan["failed"]) == 1
        assert conn.execute("SELECT state, result FROM jobs WHERE job_id=?",
                            (failed,)).fetchone() == before
        jobs.retire_backlog(conn, cutoff="2026-11-01T00:00:00+00:00")
        plan2 = jobs.retire_plan(conn, cutoff="2026-11-01T00:00:00+00:00")
        assert plan2["failed"] == [] and plan2["unterminal"] == []
        spool_before = conn.execute("SELECT COUNT(*) FROM job_spool").fetchone()[0]
        jobs.retire_backlog(conn, cutoff="2026-11-01T00:00:00+00:00")
        assert conn.execute("SELECT COUNT(*) FROM job_spool").fetchone()[0] == spool_before
    finally:
        conn.close()
