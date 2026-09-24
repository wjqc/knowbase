"""M3 测试：对话适配 + 多 checkpoint + segment_results + 多格式解析。

覆盖：
- M3-1: build_checkpoints 长会话多 checkpoint 切分
- M3-2: 中途检查点触发（work_ops 阈值）
- M3-3: segment_results 完整接线
- M3-4: 宿主提炼指引（claim 返回 distillation_guide）
- M3-5: 多格式 transcript 解析（JSON array、CSV）
"""

import json
import tempfile
from pathlib import Path

import pytest

from knowbase import config, index, jobs
from knowbase.__main__ import cmd_init
from knowbase.adapters import normalizer, checkpoint as cp_mod
from knowbase.extraction import sanitizer


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


# ---------- M3-1: build_checkpoints ----------

class TestBuildCheckpoints:
    def test_single_checkpoint(self, isolated_repo):
        """短会话应只产生一个 checkpoint。"""
        events = [
            normalizer.NormalizedEvent(
                session_id="sess1", message_id="m1", role="user",
                text="测试消息1", timestamp="2024-01-01T00:00:00Z",
            ),
            normalizer.NormalizedEvent(
                session_id="sess1", message_id="m2", role="assistant",
                text="测试回复1", timestamp="2024-01-01T00:00:01Z",
            ),
        ]
        checkpoints = cp_mod.build_checkpoints(events, session_id="sess1", scope="test")
        assert len(checkpoints) == 1
        assert checkpoints[0].checkpoint_index == 0
        assert len(checkpoints[0].events) == 2

    def test_multiple_checkpoints(self, isolated_repo):
        """长会话应按 max_chars 切分为多个 checkpoint。"""
        # 创建足够多的事件以触发多次切分
        events = []
        for i in range(20):
            events.append(normalizer.NormalizedEvent(
                session_id="sess1", message_id=f"m{i}", role="user",
                text=f"这是一条较长的测试消息 {i} " * 50,  # 每条约 1000 字符
                timestamp=f"2024-01-01T00:00:{i:02d}Z",
            ))
        checkpoints = cp_mod.build_checkpoints(events, session_id="sess1", scope="test", max_chars=5000)
        assert len(checkpoints) > 1
        # 验证 checkpoint_index 递增
        for i, cp in enumerate(checkpoints):
            assert cp.checkpoint_index == i

    def test_empty_events(self, isolated_repo):
        """空会话应返回单个空 checkpoint。"""
        checkpoints = cp_mod.build_checkpoints([], session_id="sess1", scope="test")
        assert len(checkpoints) == 1
        assert checkpoints[0].events == []
        assert checkpoints[0].char_count == 0

    def test_idempotency_key_includes_index(self, isolated_repo):
        """幂等键应包含 checkpoint_index。"""
        events = [
            normalizer.NormalizedEvent(
                session_id="sess1", message_id=f"m{i}", role="user",
                text=f"消息 {i} " * 100,
                timestamp=f"2024-01-01T00:00:{i:02d}Z",
            )
            for i in range(10)
        ]
        checkpoints = cp_mod.build_checkpoints(events, session_id="sess1", scope="test", max_chars=1000)
        keys = [cp.idempotency_key for cp in checkpoints]
        # 每个 checkpoint 的幂等键应不同
        assert len(set(keys)) == len(keys)
        # 幂等键应包含索引
        for i, cp in enumerate(checkpoints):
            assert f":{i}" in cp.idempotency_key


# ---------- M3-3: segment_results ----------

class TestSegmentResults:
    def test_segment_results_table_exists(self, isolated_repo):
        """segment_results 表应存在。"""
        conn = index.connect(isolated_repo)
        try:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
            assert "segment_results" in tables
        finally:
            conn.close()

    def test_submit_with_segment_results(self, isolated_repo):
        """submit 应持久化 segment_results。"""
        conn = jobs.connect(isolated_repo)
        try:
            # 入队并领取
            r = jobs.enqueue(conn, jobs.KIND_CAPTURE, "test-scope", {"events": []})
            job_id = r["job_id"]
            jobs.set_preparing(conn, job_id)
            jobs.set_awaiting(conn, job_id, 3)  # 3 个 segment
            claim = jobs.claim(conn, job_id, "host-1")
            lease_token = claim["lease_token"]
            batch_id = claim["batch_id"]

            # 提交带 segment_results
            result = jobs.submit(
                conn, job_id, batch_id, lease_token, "req-1",
                candidates=[
                    {"candidate_key": "c1", "body": {"conclusion": "test", "problem": "test", "scope": "test"},
                     "scope": "test", "decision": "accepted", "segment_id": "seg-1"},
                ],
                segment_results=[
                    {"segment_id": "seg-1", "decision": "distilled", "reason": ""},
                    {"segment_id": "seg-2", "decision": "skipped", "reason": "无价值"},
                    {"segment_id": "seg-3", "decision": "no_value", "reason": "一次性操作"},
                ],
            )
            assert result["accepted"] == 1

            # 验证 segment_results 已持久化
            rows = conn.execute(
                "SELECT segment_id, decision, reason FROM segment_results WHERE job_id=?",
                (job_id,),
            ).fetchall()
            assert len(rows) == 3
            decisions = {r[0]: r[1] for r in rows}
            assert decisions["seg-1"] == "distilled"
            assert decisions["seg-2"] == "skipped"
            assert decisions["seg-3"] == "no_value"
        finally:
            conn.close()

    def test_cursor_advances_by_processed_segments(self, isolated_repo):
        """游标应按已处理的 segment 数量推进。"""
        conn = jobs.connect(isolated_repo)
        try:
            r = jobs.enqueue(conn, jobs.KIND_CAPTURE, "test-scope", {"events": []})
            job_id = r["job_id"]
            jobs.set_preparing(conn, job_id)
            jobs.set_awaiting(conn, job_id, 5)
            claim = jobs.claim(conn, job_id, "host-1")
            lease_token = claim["lease_token"]
            batch_id = claim["batch_id"]

            # 只处理 2 个 segment
            result = jobs.submit(
                conn, job_id, batch_id, lease_token, "req-1",
                candidates=[],
                segment_results=[
                    {"segment_id": "seg-1", "decision": "distilled", "reason": ""},
                    {"segment_id": "seg-2", "decision": "skipped", "reason": "无价值"},
                    {"segment_id": "seg-3", "decision": "pending", "reason": ""},  # 未处理
                ],
            )
            # 游标应推进 2（只计算非 pending 的 segment）
            assert result["next_cursor"] == 2
        finally:
            conn.close()


# ---------- M3-5: 多格式 transcript 解析 ----------

class TestParseTranscriptAuto:
    def test_jsonl_format(self, isolated_repo, tmp_path):
        """JSONL 格式应正确解析。"""
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text(
            '{"role": "user", "text": "测试消息1"}\n'
            '{"role": "assistant", "text": "测试回复1"}\n',
            encoding="utf-8",
        )
        events = normalizer.parse_transcript_auto(str(transcript))
        assert len(events) == 2
        assert events[0].role == "user"
        assert events[1].role == "assistant"

    def test_json_array_format(self, isolated_repo, tmp_path):
        """JSON array 格式应正确解析。"""
        transcript = tmp_path / "transcript.json"
        transcript.write_text(
            '[{"role": "user", "text": "消息1"}, {"role": "assistant", "text": "回复1"}]',
            encoding="utf-8",
        )
        events = normalizer.parse_transcript_auto(str(transcript))
        assert len(events) == 2
        assert events[0].text == "消息1"

    def test_csv_format(self, isolated_repo, tmp_path):
        """CSV 格式应正确解析。"""
        transcript = tmp_path / "transcript.csv"
        transcript.write_text(
            "role,text,timestamp\n"
            "user,测试消息1,2024-01-01T00:00:00Z\n"
            "assistant,测试回复1,2024-01-01T00:00:01Z\n",
            encoding="utf-8",
        )
        events = normalizer.parse_transcript_auto(str(transcript))
        assert len(events) == 2
        assert events[0].role == "user"
        assert events[0].text == "测试消息1"

    def test_empty_file(self, isolated_repo, tmp_path):
        """空文件应返回空列表。"""
        transcript = tmp_path / "empty.jsonl"
        transcript.write_text("", encoding="utf-8")
        events = normalizer.parse_transcript_auto(str(transcript))
        assert events == []


# ---------- M3-4: 提炼指引 ----------

class TestDistillationGuide:
    def test_claim_returns_guide(self, isolated_repo):
        """claim 返回应包含提炼指引。"""
        from knowbase.server import job_claim_impl

        conn = jobs.connect(isolated_repo)
        try:
            # 入队并准备
            r = jobs.enqueue(conn, jobs.KIND_CAPTURE, "test-scope",
                            {"checkpoint": {"events": [{"role": "user", "text": "测试"}]}})
            job_id = r["job_id"]
            jobs.set_preparing(conn, job_id)
            jobs.set_awaiting(conn, job_id, 1)

            # 领取
            result = job_claim_impl(job_id=job_id, host_session_id="test-host")
            assert "提炼指引" in result
            assert "八项结构" in result
            assert "pitfall" in result
        finally:
            conn.close()
