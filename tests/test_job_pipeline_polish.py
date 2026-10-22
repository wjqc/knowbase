"""提炼管道修复回归：分页顺延租约 / 终态判错优先 / 待办通知过滤终态。

背景（2026-10-22 实测事故）：
- 协议要求多页读完材料 + 逐一读取相似卡后才提交，5 分钟租约易在中途到期；
- 作业被外部清理置 no_candidate 后，submit 误报 LEASE_EXPIRED 而非 already_done；
- Stop Hook 待办通知不按状态过滤，终态作业被反复催办。
"""

import json
from datetime import datetime, timedelta, timezone

from knowbase import index, jobs
from knowbase.adapters import host_events
from knowbase.server import capture_impl, job_claim_impl, job_material_impl


def _capture(text, request_id=""):
    import re
    result = capture_impl(session_id="polish-session", scope="test", request_id=request_id,
                          messages=[{"role": "user", "content": text}])
    return re.search(r"J-[a-f0-9]+", result).group()


def test_forward_page_read_extends_lease(memory_repo):
    job = _capture("分页续租规则" * 900)
    claim = json.loads(job_claim_impl(job_id=job, max_chars=200, protocol_version=2))
    assert claim["has_more"]

    conn = index.connect(memory_repo)
    near_expiry = (datetime.now(timezone.utc) + timedelta(seconds=3)).isoformat(timespec="seconds")
    conn.execute("UPDATE jobs SET lease_until=? WHERE job_id=?", (near_expiry, job))
    conn.commit()
    conn.close()

    page = json.loads(job_material_impl(job, claim["batch_id"], claim["lease_token"],
                                        cursor=claim["next_cursor"], max_chars=200))
    assert "error" not in page

    conn = index.connect(memory_repo)
    row = conn.execute("SELECT lease_until FROM jobs WHERE job_id=?", (job,)).fetchone()
    conn.close()
    assert datetime.fromisoformat(row[0]) > datetime.fromisoformat(near_expiry)
    # 顺延覆盖完整租约窗口（默认 5 分钟），而非仅几秒宽限
    assert (datetime.fromisoformat(row[0]) - datetime.now(timezone.utc)).total_seconds() > 240


def test_reread_old_page_does_not_extend_lease(memory_repo):
    job = _capture("旧页不续租规则" * 900)
    claim = json.loads(job_claim_impl(job_id=job, max_chars=200, protocol_version=2))
    first_cursor = claim["next_cursor"]
    json.loads(job_material_impl(job, claim["batch_id"], claim["lease_token"],
                                 cursor=first_cursor, max_chars=200))

    conn = index.connect(memory_repo)
    conn.execute("UPDATE jobs SET lease_until=? WHERE job_id=?",
                 ((datetime.now(timezone.utc) + timedelta(seconds=3)).isoformat(timespec="seconds"), job))
    conn.commit()
    conn.close()

    # 重复读取已交付的旧起始页：不前进游标，也不应顺延租约
    json.loads(job_material_impl(job, claim["batch_id"], claim["lease_token"],
                                 cursor=first_cursor, max_chars=200))
    conn = index.connect(memory_repo)
    row = conn.execute("SELECT lease_until FROM jobs WHERE job_id=?", (job,)).fetchone()
    conn.close()
    assert (datetime.fromisoformat(row[0]) - datetime.now(timezone.utc)).total_seconds() <= 10


def test_submit_on_terminal_job_reports_already_done(memory_repo):
    job = _capture("终态诊断规则" * 50)
    claim = json.loads(job_claim_impl(job_id=job, max_chars=2000, protocol_version=2))

    conn = index.connect(memory_repo)
    # 模拟外部清理（如积压批量处置）把持有人仍在处理的作业置为终态
    conn.execute("UPDATE jobs SET state=?, lease_until=NULL WHERE job_id=?",
                 (jobs.STATE_NO_CANDIDATE, job))
    conn.commit()
    result = jobs.submit(conn, job, claim["batch_id"], claim["lease_token"],
                         request_id="polish-terminal-1", candidates=[])
    conn.close()
    assert result == {"error": "already_done", "state": jobs.STATE_NO_CANDIDATE}


def test_pending_notifications_skip_terminal_jobs(memory_repo):
    active = _capture("活跃作业规则" * 50)
    dead = _capture("终态作业规则" * 50, request_id="dead")
    conn = index.connect(memory_repo)
    conn.execute("UPDATE jobs SET state=? WHERE job_id=?", (jobs.STATE_NO_CANDIDATE, dead))
    conn.commit()

    notices = {active: "stale", dead: "stale"}
    notified = host_events.pending_notifications(
        conn, [active, dead], notices, force=True)
    assert notified == [active]
    # 终态作业的旧签名被清理，不再遮蔽后续状态
    assert dead not in notices
    assert active in notices

    # 非强制路径：签名未变不重复催办活跃作业
    again = host_events.pending_notifications(
        conn, [active, dead], notices, force=False)
    assert again == []
    conn.close()
