"""捕获内容级去重与自身调用排除的回归测试。

背景（2026-10-22）：同一宿主会话因 input_version / source_kind 漂移被整段重复入队
（两个 28x 片段任务内容逐段相同）；transcript 路径还把 knowbase 自身的提炼工具调用
当作材料，形成提炼自我喂养循环。修复点见 adapters/host_events.py 的
_enqueued_text_hashes 与 _is_self_event。
"""
import json
from datetime import datetime, timezone

import pytest

from knowbase import config, index, materials
from knowbase.adapters import host_events
from knowbase.__main__ import cmd_init


@pytest.fixture()
def isolated_repo(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    cfg_path = tmp_path / "config.json"
    cfg = config._deep_merge(config.DEFAULTS, {
        "repo_path": str(repo),
        "knowledge_path": "",
        "git": {"auto_commit": False, "auto_push": False, "auto_pull": False},
        "inflight": {"max_pending_jobs": 8},
    })
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", cfg_path)
    monkeypatch.setenv("KNOWBASE_REPO_PATH", str(repo))
    monkeypatch.setenv("KNOWBASE_AGENT_NAME", "pytest-agent")
    assert cmd_init() == 0
    return repo


def _seed(conn, host, session, scope, events):
    """events: (event_id, observation_dict) 列表；按观察层落库的原始形态写入。"""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.execute("INSERT OR REPLACE INTO context_sessions(host_profile,session_id,scope,coverage_json,updated_at) "
                 "VALUES(?,?,?,'{}',?)", (host, session, scope, now))
    for event_id, observation in events:
        conn.execute("INSERT OR REPLACE INTO context_events(host_profile,session_id,event_id,trigger,payload_hash,"
                     "status,created_at,observation_json) VALUES(?,?,?,?,?,?,?,?)",
                     (host, session, event_id, "observe", event_id, "executed", now,
                      json.dumps(observation, ensure_ascii=False)))
    conn.commit()


def _observation(tool_name, command=""):
    return {"hook_event": "tool-after",
            "payload": {"tool_name": tool_name, "tool_input": {"command": command},
                        "toolResponse": {"stdout": "普通输出内容用于提炼判定"}}}


def _segment_count(conn):
    return conn.execute("SELECT COUNT(*) FROM job_segments").fetchone()[0]


def _payload_texts(conn, job_id):
    payload = materials.payload_for_job(conn, job_id)
    return "".join(s["text"] for s in payload["segments"])


def test_recapture_after_checkpoint_reset_is_deduped(isolated_repo):
    conn = index.connect(isolated_repo)
    try:
        _seed(conn, "zcode", "session_dedup_1", "knowbase", [
            ("event-1", _observation("Bash", "echo alpha")),
            ("event-2", _observation("Read", "")),
        ])
        first = host_events.capture_increment(isolated_repo, "session_dedup_1", "", "knowbase", "zcode")
        assert first.get("capture_status") == "queued"
        baseline = _segment_count(conn)

        # 模拟 source_kind/input_version 漂移：清空 checkpoint 触发整段重排路径。
        conn.execute("UPDATE context_sessions SET coverage_json='{}' "
                     "WHERE host_profile=? AND session_id=?", ("zcode", "session_dedup_1"))
        conn.commit()
        second = host_events.capture_increment(isolated_repo, "session_dedup_1", "", "knowbase", "zcode")
        assert second.get("capture_status") == "no_delta"
        assert _segment_count(conn) == baseline  # 无重复片段入队

        # 游标已在过滤后推进：再次触发仍无新任务。
        third = host_events.capture_increment(isolated_repo, "session_dedup_1", "", "knowbase", "zcode")
        assert third.get("capture_status") == "no_delta"
        assert _segment_count(conn) == baseline
    finally:
        conn.close()


def test_partial_recapture_only_enqueues_new_events(isolated_repo):
    conn = index.connect(isolated_repo)
    try:
        _seed(conn, "zcode", "session_dedup_2", "knowbase", [
            ("event-1", _observation("Bash", "echo alpha"))])
        assert host_events.capture_increment(
            isolated_repo, "session_dedup_2", "", "knowbase", "zcode")["capture_status"] == "queued"

        _seed(conn, "zcode", "session_dedup_2", "knowbase", [
            ("event-2", _observation("Read", ""))])
        conn.execute("UPDATE context_sessions SET coverage_json='{}' "
                     "WHERE host_profile=? AND session_id=?", ("zcode", "session_dedup_2"))
        conn.commit()
        result = host_events.capture_increment(isolated_repo, "session_dedup_2", "", "knowbase", "zcode")
        assert result["capture_status"] == "queued"
        texts = _payload_texts(conn, result["job_ids"][0])
        assert "echo alpha" not in texts  # 旧事件按片段哈希去重
        assert texts  # 新事件正常入队
    finally:
        conn.close()


def test_self_tool_events_excluded(isolated_repo):
    conn = index.connect(isolated_repo)
    try:
        _seed(conn, "zcode", "session_dedup_3", "knowbase", [
            ("event-1", _observation("mcp__knowbase__memory_job_claim", "")),
            ("event-2", _observation("Bash", "runtime/venv/bin/python -I -m knowbase hook stop")),
            ("event-3", _observation("Bash", "git status --short")),
        ])
        result = host_events.capture_increment(isolated_repo, "session_dedup_3", "", "knowbase", "zcode")
        assert result["capture_status"] == "queued"
        texts = _payload_texts(conn, result["job_ids"][0])
        assert "memory_job_claim" not in texts
        assert "-m knowbase" not in texts
        assert "git status" in texts
    finally:
        conn.close()
