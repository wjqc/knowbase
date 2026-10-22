"""正式 MCP impl 的长材料/续页、租约和迁移反例。"""

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone

from knowbase import index, jobs, materials, migrations
from knowbase.adapters.normalizer import normalize_event
from knowbase.server import capture_impl, job_claim_impl, job_material_impl


def capture(text, scope="test", request_id=""):
    result = capture_impl(session_id="long-session", scope=scope, request_id=request_id,
                          messages=[{"role": "user", "content": text}])
    return re.search(r"J-[a-f0-9]+", result).group()


def test_tail_rule_survives_normalization_and_all_pages(memory_repo):
    text = "订单规则\n" + "普通实施说明。" * 3500 + "\n尾部规则：已结算订单禁止撤回。"
    assert normalize_event({"role": "user", "content": text}).text == text
    job = capture(text)
    claim = json.loads(job_claim_impl(job_id=job, host_session_id="host", max_chars=173,
                                     protocol_version=2))
    reconstructed = "".join(m["text"] for m in claim["materials"])
    page = claim
    while page["has_more"]:
        page = json.loads(job_material_impl(job, claim["batch_id"], claim["lease_token"],
                                           cursor=page["next_cursor"], max_chars=173,
                                           fencing_token=claim["fencing_token"]))
        reconstructed += "".join(m["text"] for m in page["materials"])
    assert reconstructed == text
    assert "禁止撤回" in reconstructed
    conn = index.connect(memory_repo)
    assert jobs.status(conn, job)["cursor"] == 0  # 读完不是处理完
    assert len(page["delivered_segment_ids"]) == claim["pending_segments"]
    conn.close()


def test_tool_input_and_result_are_not_silently_truncated():
    text = "原文" * 12000
    assert text in normalize_event({"type": "tool_use", "name": "Read", "input": {"content": text}}).text
    assert normalize_event({"type": "tool_result", "content": text}).text == text
    event = normalize_event({"type": "user", "message": {"role": "user", "content": text}})
    assert event.text == text


def test_partial_page_does_not_mark_segment_delivered(memory_repo):
    job = capture("规则正文" * 1200)
    claim = json.loads(job_claim_impl(job_id=job, max_chars=50, protocol_version=2))
    assert claim["materials"][0]["truncated"]
    assert claim["delivered_segment_ids"] == []
    result = json.loads(job_material_impl(job, claim["batch_id"], claim["lease_token"], cursor="9:0"))
    assert result["error"] == "INVALID_CURSOR"


def test_expired_batch_cannot_read_even_without_takeover(memory_repo):
    job = capture("订单权限规则" * 1200)
    claim = json.loads(job_claim_impl(job_id=job, max_chars=50, protocol_version=2))
    conn = index.connect(memory_repo)
    conn.execute("UPDATE jobs SET lease_until=? WHERE job_id=?",
                 ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), job))
    conn.commit()
    conn.close()
    result = json.loads(job_material_impl(job, claim["batch_id"], claim["lease_token"]))
    assert result["error"] == "LEASE_EXPIRED"


def test_repeated_capture_keeps_existing_claim_and_manifest(memory_repo):
    job = capture("订单流程状态规则" * 1000, request_id="fixed")
    claim = json.loads(job_claim_impl(job_id=job, protocol_version=2))
    assert capture("订单流程状态规则" * 1000, request_id="fixed") == job
    conn = index.connect(memory_repo)
    assert jobs.status(conn, job)["state"] == "claimed"
    assert materials.validate_batch(conn, job, claim["batch_id"], claim["lease_token"])["segment_ids"]
    conn.close()


def test_same_segment_id_in_two_jobs_and_contiguous_coverage(memory_repo):
    conn = index.connect(memory_repo)
    payload = materials.capture_payload([normalize_event({"role": "user", "text": "业务规则" * 3000})],
                                        scope="test", session_id="session")
    for job in ["job-a", "job-b"]:
        materials.register_manifest(conn, job, payload)
        conn.execute("INSERT INTO segment_results(segment_id,job_id,batch_id,created_at,updated_at,"
                     "settlement_state) VALUES('seg-0001',?,'b','now','now','no_candidate')", (job,))
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM segment_results WHERE segment_id='seg-0001'").fetchone()[0] == 2
    assert materials.contiguous_cursor(conn, "job-a") == 0
    conn.execute("INSERT INTO segment_results(segment_id,job_id,batch_id,created_at,updated_at,"
                 "settlement_state) VALUES('seg-0000','job-a','b','now','now','no_candidate')")
    conn.commit()
    assert materials.contiguous_cursor(conn, "job-a") == 2
    assert materials.contiguous_cursor(conn, "job-b") == 0
    conn.close()


def test_upgrade_from_v8_preserves_local_logs_and_old_segment(tmp_path, monkeypatch):
    conn = sqlite3.connect(tmp_path / "old.db")
    old = migrations.MIGRATIONS[:8]
    with monkeypatch.context() as m:
        m.setattr(migrations, "MIGRATIONS", old)
        migrations.apply_all(conn)
    conn.execute("CREATE TABLE usage_log(detail TEXT)")
    conn.execute("INSERT INTO usage_log VALUES('keep')")
    conn.execute("INSERT INTO segment_results VALUES('seg-0','old-job','b','distilled','','c','a','b')")
    conn.commit()
    assert migrations.apply_all(conn) == [9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20]
    assert conn.execute("SELECT detail FROM usage_log").fetchone()[0] == "keep"
    assert conn.execute("SELECT settlement_state FROM segment_results").fetchone()[0] == "legacy_unverified"
    assert migrations.apply_all(conn) == []
    conn.close()


def test_broken_transcript_is_not_silently_accepted_as_complete(memory_repo, tmp_path):
    path = tmp_path / "broken.jsonl"
    path.write_text('{"role":"user","text":"first rule"}\nnot-json\n')
    result = capture_impl(session_id="broken", scope="test", transcript_ref=str(path))
    assert "不完整" in result
    conn = index.connect(memory_repo)
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
    conn.close()


def test_migration_checksum_mismatch_stops_before_new_schema(memory_repo, monkeypatch):
    import pytest
    conn = index.connect(memory_repo)
    changed = [(v, sql + "\n-- changed" if v == 9 else sql) for v, sql in migrations.MIGRATIONS]
    changed.append((999, "CREATE TABLE should_not_exist(id INTEGER);"))
    monkeypatch.setattr(migrations, "MIGRATIONS", changed)
    with pytest.raises(ValueError, match="checksum"):
        migrations.apply_all(conn)
    assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='should_not_exist'").fetchone()
    conn.close()
