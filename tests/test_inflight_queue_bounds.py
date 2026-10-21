import json
from concurrent.futures import ThreadPoolExecutor

from knowbase import config, index, jobs
from knowbase.server import capture_impl, import_impl


def limit_jobs(limit):
    cfg = json.loads(config.CONFIG_PATH.read_text())
    cfg.setdefault("inflight", {})["max_pending_jobs"] = limit
    config.CONFIG_PATH.write_text(json.dumps(cfg))


def test_concurrent_same_request_creates_exactly_one_task(memory_repo):
    def add(_):
        conn = index.connect(memory_repo)
        try:
            return jobs.enqueue(conn, "capture", "orders", {"text": "same"}, idempotency_key="one-key")
        finally:
            conn.close()
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(add, range(12)))
    assert sum(r["created"] for r in results) == 1
    assert len({r["job_id"] for r in results}) == 1


def test_backpressure_is_atomic_and_keeps_accepted_inputs(memory_repo):
    limit_jobs(3)
    def add(number):
        conn = index.connect(memory_repo)
        try:
            return jobs.enqueue(conn, "capture", "orders", {"text": str(number)}, idempotency_key=f"key-{number}")
        finally:
            conn.close()
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(add, range(10)))
    assert sum(r["created"] for r in results) == 3
    assert sum(r.get("error") == "BACKLOG_FULL" for r in results) == 7
    conn = index.connect(memory_repo)
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 3
    assert conn.execute("SELECT COUNT(*) FROM job_spool").fetchone()[0] == 3
    conn.close()


def test_capture_and_import_report_full_queue_without_false_success(memory_repo, tmp_path):
    limit_jobs(0)
    assert "BACKLOG_FULL" in capture_impl(session_id="s", scope="orders", messages=[{"role": "user", "text": "规则"}])
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "rules.md").write_text("# 订单规则\n已结算订单禁止撤回。")
    result = import_impl(str(docs), scope="orders")
    assert "BACKLOG_FULL" in result and "未入队" in result


def test_next_material_batch_does_not_consume_failure_retry_budget(memory_repo):
    conn = index.connect(memory_repo)
    job = jobs.enqueue(conn, "capture", "orders", {"text": "long"})["job_id"]
    jobs.set_preparing(conn, job)
    jobs.set_awaiting(conn, job, 4)
    for number in range(4):
        claim = jobs.claim(conn, job, "host")
        assert not claim.get("error"), claim
        jobs.submit(conn, job, claim["batch_id"], claim["lease_token"], f"batch-{number}", [],
                    [{"segment_id": f"seg-{number}", "decision": "no_value", "reason": "一次性进度"}])
        assert jobs.status(conn, job)["attempt"] == 1
    conn.close()
