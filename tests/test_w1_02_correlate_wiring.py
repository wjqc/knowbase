"""W1-2：待归因 run 自动归因接线（observation.pending_runs + worker 任务）。

覆盖：pending_runs 只列有递送且无证据的 run（无递送不列，防每轮空转）；
correlate 产 L0/L1/L2 后 pending 自然消除；worker handler 逐 run 容错与幂等；
门关时 skipped；经 tick 全链路真实调度执行一次。
"""
import json

from knowbase import config, index, writer
from knowbase.automation import worker
from knowbase.knowledge_lifecycle import attribution, observation
from knowbase.migrations import apply_all

BODY = ("## 结论\n归因链以动作为准。\n## 解决的问题\nx。\n"
        "## 适用条件\ny。\n## 不适用条件\nz。\n## 可执行动作\n运行 scripts/settle.py。\n"
        "## 关键证据\nPR。\n## 验证情况\nv。\n## 未知与待确认\n无。\n")


def _head_of(conn, mid):
    return conn.execute(
        "SELECT content_revision FROM entity_heads WHERE entity_id=?", (mid,)).fetchone()[0]


def _seed_run(conn, repo, mid, revision, host, session, *, command,
              with_delivery=True, read=False):
    """真实路径种观测：ingest 落 native_events/task_runs；递送/uses 直插。"""
    ingested = observation.ingest(
        conn, host, session,
        [{"native_key": f"{session}:n1", "kind": "tool_call", "tool_name": "Bash",
          "tool_call_id": "c1", "payload": {"command": command}}],
        project_key="repo:test")
    assert not ingested.get("error"), ingested
    if with_delivery:
        conn.execute(
            "INSERT INTO retrieval_deliveries(delivery_id,search_id,host_profile,"
            "session_id,scope,memory_id,content_revision,rank,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (f"D-{session}", f"S-{session}", host, session, "orders",
             mid, revision, 1, "2026-11-05T00:00:00"))
    if read:
        conn.execute(
            "INSERT INTO knowledge_uses(use_id,host_profile,session_id,event_id,"
            "payload_hash,context_event_id,task_id,task_revision,scope,memory_id,"
            "content_revision,state,current_version,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"U-{session}", host, session, f"E-{session}", "h", "", "", "",
             "orders", mid, revision, "read", 1, "2026-11-05T00:00:00"))
    conn.commit()
    return ingested["run_id"]


def test_pending_runs_requires_delivery_and_absence_of_evidence(memory_repo):
    card = writer.create(memory_repo, "pitfall", "结算动作卡", BODY, scope="orders")
    assert not card.get("error"), card
    mid = card["entity_id"]
    conn = index.connect(memory_repo)
    apply_all(conn)
    revision = _head_of(conn, mid)
    run_main = _seed_run(conn, memory_repo, mid, revision, "zcode", "cs1",
                         command="python scripts/settle.py --dry-run", read=True)
    run_bare = _seed_run(conn, memory_repo, mid, revision, "zcode", "cs-bare",
                         command="ls", with_delivery=False)
    pending = observation.pending_runs(conn)
    assert [p["run_id"] for p in pending] == [run_main], pending
    result = attribution.correlate(conn, memory_repo, run_main)
    assert not result.get("error"), result
    assert result["L0"] == [mid] and result["L1"] == [mid] and result["L2"] == [mid]
    # 已归因 → 不再 pending；无递送的 run 永不进入清单。
    assert observation.pending_runs(conn) == []
    assert conn.execute("SELECT COUNT(*) FROM evidence_events WHERE run_id=?",
                        (run_bare,)).fetchone()[0] == 0
    conn.close()


def test_correlate_handler_idempotent_and_gated(memory_repo):
    card = writer.create(memory_repo, "pitfall", "结算动作卡", BODY, scope="orders")
    assert not card.get("error"), card
    mid = card["entity_id"]
    conn = index.connect(memory_repo)
    apply_all(conn)
    revision = _head_of(conn, mid)
    _seed_run(conn, memory_repo, mid, revision, "zcode", "cs2",
              command="python scripts/settle.py --dry-run", read=True)
    cfg = config.load_config()
    first = worker._h_lifecycle_correlate(memory_repo, conn, cfg)
    assert first["correlated"] == 1 and first["L0"] == 1 and first["L2"] == 1, first
    # 幂等：证据已存在 → 无 pending → 零增量。
    second = worker._h_lifecycle_correlate(memory_repo, conn, cfg)
    assert second["correlated"] == 0 and second["L0"] == 0, second
    # 门关 → skipped（不触碰数据）。
    cfg_off = json.loads(json.dumps(cfg))
    cfg_off["knowledge_lifecycle"]["worker"]["correlate"] = False
    assert worker._h_lifecycle_correlate(
        memory_repo, conn, cfg_off) == {"skipped": "lifecycle_correlate_disabled"}
    conn.close()


def test_correlate_scheduled_by_tick(memory_repo):
    card = writer.create(memory_repo, "pitfall", "结算动作卡", BODY, scope="orders")
    assert not card.get("error"), card
    mid = card["entity_id"]
    conn = index.connect(memory_repo)
    apply_all(conn)
    revision = _head_of(conn, mid)
    _seed_run(conn, memory_repo, mid, revision, "zcode", "cs3",
              command="python scripts/settle.py --dry-run", read=True)
    conn.close()
    result = worker.tick(memory_repo)
    ran = {r["kind"]: r for r in result.get("ran", [])}
    assert "lifecycle_correlate" in ran, ran
    assert ran["lifecycle_correlate"]["status"] == "success", ran["lifecycle_correlate"]
    conn = index.connect(memory_repo)
    levels = conn.execute(
        "SELECT level,COUNT(*) FROM evidence_events GROUP BY level").fetchall()
    assert dict(levels) == {"L0": 1, "L1": 1, "L2": 1}, levels
    conn.close()
