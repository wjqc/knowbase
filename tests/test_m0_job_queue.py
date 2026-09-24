"""M0 任务队列与提炼流水线测试。

覆盖：
- 数据库迁移（migrations）
- 任务队列完整生命周期（jobs）
- 脱敏、切分、校验（extraction）
- transcript 归一化与 checkpoint（adapters）
- Stop Hook 入队集成
- MCP 工具 capture/claim/renew/submit/status
- CLI jobs 子命令
"""

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from knowbase import config, index, jobs, migrations, store
from knowbase.__main__ import cmd_init, cmd_jobs_list, cmd_jobs_status, cmd_jobs_retry, cmd_jobs_expire
from knowbase.adapters import normalizer, checkpoint as cp_mod
from knowbase.extraction import sanitizer, segmenter, validator


@pytest.fixture()
def isolated_repo(tmp_path: Path, monkeypatch):
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


# ---------- migrations ----------

class TestMigrations:
    def test_apply_all_creates_tables(self, isolated_repo):
        conn = index.connect(isolated_repo)
        try:
            applied = migrations.apply_all(conn)
            # 首次连接已自动 apply，再次应为空
            assert migrations.apply_all(conn) == []
            # 验证表存在
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
            assert "jobs" in tables
            assert "candidates" in tables
            assert "sync_attempts" in tables
            assert "sync_lease" in tables
            assert "job_spool" in tables
            assert "schema_migrations" in tables
        finally:
            conn.close()

    def test_verify_checksum(self, isolated_repo):
        conn = index.connect(isolated_repo)
        try:
            errors = migrations.verify(conn)
            assert errors == []
        finally:
            conn.close()


# ---------- jobs ----------

class TestJobs:
    def test_enqueue_and_idempotency(self, isolated_repo):
        conn = index.connect(isolated_repo)
        try:
            r1 = jobs.enqueue(conn, jobs.KIND_CAPTURE, "proj-a", {"text": "hello"})
            assert r1["created"] is True
            assert r1["state"] == jobs.STATE_QUEUED

            # 同 key 幂等
            r2 = jobs.enqueue(conn, jobs.KIND_CAPTURE, "proj-a", {"text": "hello"})
            assert r2["created"] is False
            assert r2["job_id"] == r1["job_id"]
        finally:
            conn.close()

    def test_full_lifecycle(self, isolated_repo):
        conn = index.connect(isolated_repo)
        try:
            # 入队
            r = jobs.enqueue(conn, jobs.KIND_CAPTURE, "proj-a", {"events": [{"role": "user", "text": "test"}]})
            job_id = r["job_id"]

            # queued → preparing → awaiting
            assert jobs.set_preparing(conn, job_id)
            assert jobs.set_awaiting(conn, job_id, 1)

            # claim
            claim = jobs.claim(conn, job_id, "host-1")
            assert "error" not in claim
            assert claim["fencing_token"] == 1
            lease_token = claim["lease_token"]
            batch_id = claim["batch_id"]

            # renew
            renewed = jobs.renew(conn, job_id, lease_token)
            assert "error" not in renewed

            # submit candidates
            result = jobs.submit(conn, job_id, batch_id, lease_token, "req-1",
                                 [{"candidate_key": "c1", "body": {"conclusion": "test",
                                                                    "problem": "test", "scope": "proj-a"},
                                   "scope": "proj-a", "decision": "accepted"}])
            assert result["accepted"] == 1
            assert result["state"] == jobs.STATE_SUCCEEDED
        finally:
            conn.close()

    def test_claim_lease_expiry(self, isolated_repo):
        conn = index.connect(isolated_repo)
        try:
            r = jobs.enqueue(conn, jobs.KIND_CAPTURE, "proj-a", {"data": "x"})
            job_id = r["job_id"]
            jobs.set_preparing(conn, job_id)
            jobs.set_awaiting(conn, job_id, 1)

            # 首次 claim
            c1 = jobs.claim(conn, job_id, "host-1")
            assert "error" not in c1

            # 手动将 lease_until 设为过去时间，模拟租约过期
            past = "2000-01-01T00:00:00"
            conn.execute("UPDATE jobs SET lease_until=? WHERE job_id=?", (past, job_id))
            conn.commit()

            c2 = jobs.claim(conn, job_id, "host-2")
            assert "error" not in c2
            assert c2["fencing_token"] > c1["fencing_token"]
        finally:
            conn.close()

    def test_old_lease_rejected(self, isolated_repo):
        conn = index.connect(isolated_repo)
        try:
            r = jobs.enqueue(conn, jobs.KIND_CAPTURE, "proj-a", {"data": "x"})
            job_id = r["job_id"]
            jobs.set_preparing(conn, job_id)
            jobs.set_awaiting(conn, job_id, 1)

            c1 = jobs.claim(conn, job_id, "host-1")
            old_token = c1["lease_token"]

            # 手动将 lease_until 设为过去时间，模拟租约过期
            past = "2000-01-01T00:00:00"
            conn.execute("UPDATE jobs SET lease_until=? WHERE job_id=?", (past, job_id))
            conn.commit()

            # host-2 接管
            c2 = jobs.claim(conn, job_id, "host-2")
            assert "error" not in c2

            # host-1 迟到提交应被拒绝
            result = jobs.submit(conn, job_id, c1["batch_id"], old_token, "req-old", [])
            assert result["error"] == jobs.LEASE_EXPIRED
        finally:
            conn.close()

    def test_submit_no_candidate(self, isolated_repo):
        conn = index.connect(isolated_repo)
        try:
            r = jobs.enqueue(conn, jobs.KIND_CAPTURE, "proj-a", {"data": "x"})
            job_id = r["job_id"]
            jobs.set_preparing(conn, job_id)
            jobs.set_awaiting(conn, job_id, 1)

            c = jobs.claim(conn, job_id, "host-1")
            result = jobs.submit_no_candidate(conn, job_id, c["lease_token"], "req-1", "no experience")
            assert result["state"] == jobs.STATE_NO_CANDIDATE
        finally:
            conn.close()

    def test_fail_and_retry(self, isolated_repo):
        conn = index.connect(isolated_repo)
        try:
            r = jobs.enqueue(conn, jobs.KIND_CAPTURE, "proj-a", {"data": "x"}, max_attempts=2)
            job_id = r["job_id"]

            # 失败
            f = jobs.fail(conn, job_id, "parse error", retryable=True)
            assert f["state"] == jobs.STATE_RETRY_WAIT

            # 重试
            rt = jobs.retry(conn, job_id)
            assert rt["state"] == jobs.STATE_QUEUED
        finally:
            conn.close()

    def test_expire_leases(self, isolated_repo):
        conn = index.connect(isolated_repo)
        try:
            r = jobs.enqueue(conn, jobs.KIND_CAPTURE, "proj-a", {"data": "x"})
            job_id = r["job_id"]
            jobs.set_preparing(conn, job_id)
            jobs.set_awaiting(conn, job_id, 1)

            # claim 后手动设置过期
            jobs.claim(conn, job_id, "host-1")
            conn.execute("UPDATE jobs SET lease_until=? WHERE job_id=?",
                         ("2000-01-01T00:00:00", job_id))
            conn.commit()

            n = jobs.expire_leases(conn)
            assert n == 1

            s = jobs.status(conn, job_id)
            assert s["state"] == jobs.STATE_AWAITING
        finally:
            conn.close()

    def test_list_jobs(self, isolated_repo):
        conn = index.connect(isolated_repo)
        try:
            jobs.enqueue(conn, jobs.KIND_CAPTURE, "proj-a", {"data": "1"})
            jobs.enqueue(conn, jobs.KIND_CAPTURE, "proj-b", {"data": "2"})

            all_jobs = jobs.list_jobs(conn)
            assert len(all_jobs) == 2

            proj_a = jobs.list_jobs(conn, scope="proj-a")
            assert len(proj_a) == 1
        finally:
            conn.close()


# ---------- extraction ----------

class TestSanitizer:
    def test_redacts_api_key(self):
        text = 'config: api_key=sk-1234567890abcdef'
        result = sanitizer.sanitize(text)
        assert "sk-1234567890abcdef" not in result
        assert "REDACTED" in result

    def test_redacts_email(self):
        text = "contact user@example.com for help"
        result = sanitizer.sanitize(text)
        assert "user@example.com" not in result

    def test_redacts_machine_path(self):
        text = "file at /Users/john/project/config.py"
        result = sanitizer.sanitize(text)
        assert "/Users/john" not in result

    def test_has_credentials(self):
        assert sanitizer.has_credentials("password=secret123")
        assert not sanitizer.has_credentials("hello world")


class TestSegmenter:
    def test_split_markdown_by_headings(self):
        text = "# Title\n\nIntro\n\n## Section 1\n\nContent 1\n\n## Section 2\n\nContent 2"
        segs = segmenter.segment_text(text, source_format="markdown")
        assert len(segs) >= 2
        assert all(s.text_hash for s in segs)

    def test_split_by_paragraphs(self):
        text = "Paragraph 1\n\nParagraph 2\n\nParagraph 3"
        segs = segmenter.segment_text(text, source_format="txt")
        assert len(segs) == 3

    def test_empty_text(self):
        assert segmenter.segment_text("") == []

    def test_long_text_split(self):
        text = "A" * 10000
        segs = segmenter.segment_text(text, max_chars=4000, source_format="txt")
        assert len(segs) > 1
        assert all(len(s.text) <= 5000 for s in segs)  # 允许 overlap


class TestValidator:
    def test_valid_candidate(self):
        c = {"body": {"conclusion": "test", "problem": "test", "scope": "proj"},
             "type": "pitfall", "scope": "proj", "evidence": ["e1"]}
        result = validator.validate_candidate(c)
        assert result.ok

    def test_missing_required_fields(self):
        c = {"body": {"conclusion": "test"}, "type": "pitfall"}
        result = validator.validate_candidate(c)
        assert not result.ok
        assert any("problem" in e for e in result.errors)

    def test_invalid_type(self):
        c = {"body": {"conclusion": "x", "problem": "x"}, "type": "invalid"}
        result = validator.validate_candidate(c)
        assert not result.ok

    def test_rejects_verified_confidence(self):
        c = {"body": {"conclusion": "x", "problem": "x"}, "confidence": "verified"}
        result = validator.validate_candidate(c)
        assert not result.ok

    def test_build_card_body(self):
        c = {"body": {"conclusion": "结论内容", "problem": "问题描述",
                       "applicable_when": "条件", "actionable_steps": "步骤"}}
        body = validator.build_card_body(c)
        assert "## 结论" in body
        assert "结论内容" in body


# ---------- adapters ----------

class TestNormalizer:
    def test_normalize_user_message(self):
        raw = {"role": "user", "content": "hello world", "session_id": "s1"}
        ev = normalizer.normalize_event(raw)
        assert ev.role == "user"
        assert ev.text == "hello world"
        assert ev.session_id == "s1"

    def test_normalize_tool_use(self):
        raw = {"type": "tool_use", "name": "memory_save", "id": "t1",
               "input": {"title": "test"}}
        ev = normalizer.normalize_event(raw)
        assert ev.role == "tool"
        assert ev.tool_name == "memory_save"

    def test_normalize_content_list(self):
        raw = {"role": "assistant", "content": [
            {"type": "text", "text": "hello"},
            {"type": "tool_use", "name": "edit", "id": "t2"},
        ]}
        ev = normalizer.normalize_event(raw)
        assert "hello" in ev.text
        assert ev.tool_name == "edit"


class TestCheckpoint:
    def test_build_checkpoint(self):
        events = [
            normalizer.NormalizedEvent(session_id="s1", message_id="m1",
                                       role="user", text="fix docker issue"),
            normalizer.NormalizedEvent(session_id="s1", message_id="m2",
                                       role="assistant", text="try docker restart"),
        ]
        cp = cp_mod.build_checkpoint(events, session_id="s1", scope="proj-a")
        assert cp.session_id == "s1"
        assert cp.scope == "proj-a"
        assert cp.char_count > 0
        assert cp.checkpoint_hash
        assert "capture:" in cp.idempotency_key

    def test_checkpoint_sanitizes(self):
        events = [
            normalizer.NormalizedEvent(session_id="s1", message_id="m1",
                                       role="user", text="api_key=sk-secret123456"),
        ]
        cp = cp_mod.build_checkpoint(events, session_id="s1", scope="proj")
        assert "sk-secret123456" not in json.dumps(cp.events)


# ---------- hooks integration ----------

class TestHooksJobIntegration:
    def test_stop_event_enqueues_job(self, isolated_repo, tmp_path):
        """Stop Hook 检测到有操作无沉淀时入队 capture 任务。"""
        from knowbase.hooks import stop_event, _flag_path

        # 创建模拟 transcript
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text(
            json.dumps({"type": "tool_use", "name": "Edit", "input": {"file": "x.py"}}) + "\n"
            + json.dumps({"role": "assistant", "content": "fixed it"}) + "\n",
            encoding="utf-8",
        )

        # 清除 flag
        flag = _flag_path("test-session")
        if flag.exists():
            flag.unlink()

        result = stop_event("test-session", str(transcript))
        assert result  # 应该阻断
        parsed = json.loads(result)
        assert parsed["decision"] == "block"
        # 应包含 job_id 或手动沉淀提示
        assert "memory_save" in parsed["reason"] or "memory_job_claim" in parsed["reason"]


# ---------- MCP tools ----------

class TestMCPCaptureTools:
    def test_capture_and_claim(self, isolated_repo):
        from knowbase.server import capture_impl, job_claim_impl, job_status_impl

        # capture
        result = capture_impl(
            session_id="test-session",
            scope="proj-a",
            messages=[{"role": "user", "content": "docker compose up failed with port conflict"}],
        )
        assert "已入队" in result or "任务已存在" in result

        # 提取 job_id
        if "已入队" in result:
            job_id = result.split("已入队 ")[1].split("（")[0]

            # status
            status = job_status_impl(job_id=job_id)
            assert "awaiting_agent" in status

            # claim
            claim = job_claim_impl(job_id=job_id, host_session_id="test-host")
            assert "批次" in claim
            assert "lease_token" in claim

    def test_capture_requires_scope(self, isolated_repo):
        from knowbase.server import capture_impl
        result = capture_impl(session_id="s1")
        assert "错误" in result


# ---------- CLI ----------

class TestCLIJobs:
    def test_jobs_list_empty(self, isolated_repo, capsys):
        assert cmd_jobs_list() == 0
        captured = capsys.readouterr()
        assert "无任务" in captured.out

    def test_jobs_list_with_jobs(self, isolated_repo, capsys):
        conn = index.connect(isolated_repo)
        jobs.enqueue(conn, jobs.KIND_CAPTURE, "proj-a", {"data": "x"})
        conn.close()

        assert cmd_jobs_list() == 0
        captured = capsys.readouterr()
        assert "capture" in captured.out

    def test_jobs_status(self, isolated_repo, capsys):
        conn = index.connect(isolated_repo)
        r = jobs.enqueue(conn, jobs.KIND_CAPTURE, "proj-a", {"data": "x"})
        conn.close()

        assert cmd_jobs_status(r["job_id"]) == 0
        captured = capsys.readouterr()
        assert r["job_id"] in captured.out

    def test_jobs_retry(self, isolated_repo, capsys):
        conn = index.connect(isolated_repo)
        r = jobs.enqueue(conn, jobs.KIND_CAPTURE, "proj-a", {"data": "x"})
        jobs.fail(conn, r["job_id"], "test error", retryable=True)
        conn.close()

        assert cmd_jobs_retry(r["job_id"]) == 0
        captured = capsys.readouterr()
        assert "queued" in captured.out

    def test_jobs_expire(self, isolated_repo, capsys):
        assert cmd_jobs_expire() == 0
        captured = capsys.readouterr()
        assert "回收" in captured.out
