"""Regressions for the host and lifecycle paths used by the rollout."""

import json
from datetime import datetime, timedelta, timezone

from knowbase import __main__ as cli, config, index, jobs, store, writer
from knowbase.adapters import checkpoint_queue
from knowbase.knowledge_lifecycle import attribution, changes, publication, validation, validators
from knowbase.server import capture_impl, job_claim_impl, job_status_impl, update_impl
from test_t2_unified_lifecycle import BODY


def test_mcp_revision_waits_for_review_and_keeps_published_head(memory_repo):
    card = writer.create(memory_repo, "workflow", "变更需要审核", BODY, scope="orders",
                         provenance="spec §1", push=False)
    mid, base = card["entity_id"], card["content_revision"]
    body = BODY.replace("受控样本结论", "需要审核的新结论")
    reply = update_impl(mid, body=body, request_id="edit-1", expected_content_revision=base)
    assert "修订已提交待审" in reply, reply
    change_id = reply.split("：", 1)[1].split("；", 1)[0]
    assert update_impl(mid, body=body, request_id="edit-1", expected_content_revision=base) == reply
    assert "需要审核的新结论" not in store.load(memory_repo, mid)[1]
    with index.connect(memory_repo) as conn:
        assert changes.get_change(conn, change_id)["risk"] == "R2"
        assert publication.publish(memory_repo, conn, change_id)["error"] == "NOT_READY"
        assert validation.validate_change(conn, memory_repo, change_id)["result"] == "fail"
        report = validators.parse_pytest_text(validators.stamp_report_text(
            "PASSED tests/sample.py::test_example", memory_id=mid, content_revision=base,
            validator_id="pytest-text"))
        report["target"] = {"memory_id": mid, "content_revision": base}
        attribution.record_execution(conn, memory_id=mid, content_revision=base,
                                     artifact_hash=report["artifact_hash"], validator_id="pytest-text")
        assert attribution.verify_execution(conn, memory_repo, run_id="", validator_id="pytest-text",
                                            report=report)["outcome"] == "pass"
        result = validation.validate_change(conn, memory_repo, change_id)
        assert result["result"] == "pass", result
        assert publication.publish(memory_repo, conn, change_id)["local_published"]
    assert "需要审核的新结论" in store.load(memory_repo, mid)[1]


def test_scope_discovery_and_claim_include_due_retry(memory_repo):
    import re

    ids = []
    for name in ("due", "later", "review"):
        reply = capture_impl(session_id=name, scope="orders", messages=[{"role": "user", "text": name}])
        ids.append(re.search(r"J-[0-9a-f]+", reply).group())
    with index.connect(memory_repo) as conn:
        conn.executemany("UPDATE jobs SET state=?,next_run_at=? WHERE job_id=?", [
            ("retry_wait", "2000-01-01T00:00:00+00:00", ids[0]),
            ("retry_wait", "2999-01-01T00:00:00+00:00", ids[1]),
            ("needs_review", "", ids[2]),
        ])
    assert [j["job_id"] for j in json.loads(job_status_impl(scope="orders"))["jobs"]] == [ids[0]]
    assert [j["job_id"] for j in json.loads(job_status_impl(scope="orders", state="needs_review"))["jobs"]] == [ids[2]]
    assert json.loads(job_claim_impl(scope="orders", protocol_version=2))["job_id"] == ids[0]


def test_manual_shared_publication_requires_confirmation(memory_repo):
    card = writer.create(memory_repo, "workflow", "共享卡需记录审核", BODY, scope="global",
                         provenance="spec §1", push=False)
    mid = card["entity_id"]
    assert cli.cmd_promote(mid) == 1
    assert store.load(memory_repo, mid)[2].parent.name == "staging"
    assert cli.cmd_verify(mid) == 0
    assert cli.cmd_promote(mid, authority_ref="spec §2 审核记录") == 0
    meta, _, path = store.load(memory_repo, mid)
    assert path.parent.name == "workflows"
    assert (meta["status"], meta["confidence"]) == ("active", "once")


def test_shared_revision_confirmation_cannot_target_the_old_head(memory_repo):
    card = writer.create(memory_repo, "workflow", "共享修订审核版本", BODY, scope="global",
                         provenance="spec §1", push=False)
    mid, base = card["entity_id"], card["content_revision"]
    assert cli.cmd_promote(mid, authority_ref="spec §1 审核记录") == 0
    response = update_impl(mid, body=BODY + "\n更新后的正文", expected_content_revision=base)
    change_id = response.split("：", 1)[1].split("；", 1)[0]
    with index.connect(memory_repo) as conn:
        from knowbase.knowledge_lifecycle import admission
        assert admission.authority_confirm(conn, memory_repo, change_id, mid, base,
                                           authority_ref="只审核过旧稿")["error"] == "REVISION_CONFLICT"


def test_pending_replacement_does_not_hide_an_active_card(memory_repo):
    original = writer.create(memory_repo, "workflow", "原有流程保留可见", BODY,
                             scope="orders", provenance="spec §1", push=False)
    mid = original["entity_id"]
    candidate = writer.create(memory_repo, "pitfall", "拟议的新排查流程", BODY,
                              scope="orders", provenance="spec §2", push=False,
                              relations=[{"id": mid, "type": "supersedes"}])
    assert candidate["staging"]
    assert store.load(memory_repo, mid)[0]["status"] == "active"


def test_registered_report_cannot_be_replayed_under_another_run(memory_repo):
    card = writer.create(memory_repo, "workflow", "执行身份需匹配", BODY, scope="orders",
                         provenance="spec §1", push=False)
    mid, revision = card["entity_id"], card["content_revision"]
    report = validators.parse_pytest_text(validators.stamp_report_text(
        "PASSED tests/sample.py::test_example", memory_id=mid, content_revision=revision,
        run_id="run-a", validator_id="pytest-text"))
    report["target"] = {"memory_id": mid, "content_revision": revision}
    with index.connect(memory_repo) as conn:
        attribution.record_execution(conn, memory_id=mid, content_revision=revision,
                                     artifact_hash=report["artifact_hash"], run_id="run-a",
                                     validator_id="pytest-text")
        assert attribution.verify_execution(conn, memory_repo, run_id="run-b",
                                            validator_id="pytest-text", report=report)["error"] == "EXECUTION_IDENTITY_MISMATCH"
        assert conn.execute("SELECT count(*) FROM evidence_events").fetchone()[0] == 0
        assert attribution.verify_execution(conn, memory_repo, run_id="run-a",
                                            validator_id="pytest-text", report=report)["outcome"] == "pass"


def test_retirement_respects_cutoff_and_unfinished_submission(memory_repo):
    with index.connect(memory_repo) as conn:
        ids = [jobs.enqueue(conn, jobs.KIND_CAPTURE, "orders", {"sample": i})["job_id"] for i in range(3)]
        now = datetime.now(timezone.utc)
        old, cutoff = [(now - timedelta(days=d)).isoformat(timespec="seconds") for d in (20, 10)]
        conn.execute("UPDATE jobs SET state='failed'")
        conn.executemany("UPDATE jobs SET created_at=? WHERE job_id=?", [(old, ids[0]), (old, ids[1])])
        conn.execute("INSERT INTO job_submissions(job_id,request_id,batch_id,payload_hash,state,created_at,updated_at)"
                     " VALUES(?,?,?,?,?,?,?)", (ids[1], "req", "batch", "hash", "applying", old, old))
        conn.commit()
        receipt = jobs.retire_backlog(conn, cutoff=cutoff)
        assert [r["job_id"] for r in receipt["jobs"]] == [ids[0]]
        assert jobs.retry(conn, ids[0])["error"] == "RETIRED_BACKLOG"
        assert {row[0] for row in conn.execute("SELECT job_id FROM job_spool")} == set(ids[1:])


def test_retirement_waits_for_checkpoint_worker(memory_repo):
    from knowbase.automation.worker import acquire_lease
    with index.connect(memory_repo) as conn:
        pending = jobs.enqueue(conn, jobs.KIND_CHECKPOINT, "orders", {"sample": 1})
        assert acquire_lease(conn, "fixture-worker")
        cutoff = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(timespec="seconds")
        assert jobs.retire_backlog(conn, cutoff=cutoff)["error"] == "WORKER_ACTIVE"
        assert jobs.status(conn, pending["job_id"])["state"] == "queued"


def test_checkpoint_redacts_before_storage_and_recovers_inflight_tail(memory_repo, tmp_path):
    path = tmp_path / "session.jsonl"
    secret = "synthetic-" + "checkpoint-secret-123456789"
    path.write_text(json.dumps({"role": "user", "text": f"password={secret}\n独立结论可复用"},
                               ensure_ascii=False) + "\n", encoding="utf-8")
    checkpoint = checkpoint_queue.enqueue(memory_repo, "test-session", str(path), "orders", "zcode")
    path.unlink()
    with index.connect(memory_repo) as conn:
        raw = conn.execute("SELECT input_json FROM job_spool WHERE job_id=?", (checkpoint["checkpoint_id"],)).fetchone()[0]
        assert secret not in raw
        conn.execute("INSERT INTO context_sessions(host_profile,session_id,scope,updated_at) VALUES(?,?,?,?)",
                     ("zcode", "test-session", "orders", datetime.now(timezone.utc).isoformat()))
        conn.commit()
        result = checkpoint_queue.run_due(memory_repo, conn, config.load_config(), lambda: True)["results"][0]
        assert result["job_ids"], result
        payload = conn.execute("SELECT input_json FROM job_spool WHERE job_id=?", (result["job_ids"][0],)).fetchone()[0]
        assert "独立结论可复用" in payload and secret not in payload
