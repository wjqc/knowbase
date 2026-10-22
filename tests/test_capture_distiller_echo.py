"""提炼会话自回声源头过滤回归。

背景（2026-10-22）：处理提炼作业的会话，每轮 assistant 回复被 Stop 捕获成
1 段材料并入队，形成"处理→催办→回复→再入队"死循环；当天实测一条回复即一个
新作业。修复：会话实际调用 memory_job_* 即粘性标记为提炼会话（handle 落标 +
transcript 路径兜底扫描），此后该会话 assistant 角色段全部不入队，工具载荷
照常采集。
"""
import json
from datetime import datetime, timezone

import pytest

from knowbase import config, index
from knowbase.adapters import host_events
from knowbase.adapters.normalizer import NormalizedEvent
from knowbase.__main__ import cmd_init


@pytest.fixture()
def isolated_repo(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(config._deep_merge(config.DEFAULTS, {
        "repo_path": str(repo), "knowledge_path": "",
        "git": {"auto_commit": False, "auto_push": False, "auto_pull": False},
        "inflight": {"enabled": True, "capture_enabled": True, "max_pending_jobs": 8},
    })), encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", cfg_path)
    monkeypatch.setenv("KNOWBASE_REPO_PATH", str(repo))
    monkeypatch.setenv("KNOWBASE_AGENT_NAME", "pytest-agent")
    assert cmd_init() == 0
    return repo


def _event(role, text, tool_name=""):
    return NormalizedEvent(session_id="s", message_id="m", role=role, text=text, tool_name=tool_name)


class TestIsDistillerOp:
    def test_structured_tool_name_variants(self):
        assert host_events._is_distiller_op(_event("tool", "x", "mcp__knowbase__memory_job_claim"))
        assert host_events._is_distiller_op(_event("tool", "x", "memory_job_submit"))
        # 常规读写工具不算：memory_search/save 是正常使用，不构成提炼循环
        assert not host_events._is_distiller_op(_event("tool", "x", "mcp__knowbase__memory_search"))
        assert not host_events._is_distiller_op(_event("tool", "x", "memory_save"))

    def test_escaped_json_tool_name_in_text(self):
        raw = r'{"tool_name": \"memory_job_material\", "payload": 1}'
        assert host_events._is_distiller_op(_event("tool", raw))

    def test_prose_mention_does_not_count(self):
        # 催办提示正文会出现 memory_job_ 字样，不得触发过滤
        prose = "[knowbase] 请 memory_job_claim(protocol_version=2) 领取，memory_job_material 续页"
        assert not host_events._is_distiller_op(_event("assistant", prose))
        assert not host_events._is_distiller_op(_event("user", "用 memory_job_submit 提交"))


class TestDropDistillerEcho:
    def test_flagged_session_drops_assistant_keeps_tool(self):
        delta = [_event("assistant", "本轮提炼待办处理完毕……"),
                 _event("tool", '{"stdout": "真实工具证据"}'),
                 _event("user", "B方案执行")]
        coverage = {"_distiller_session": True}
        kept = host_events._drop_distiller_echo(delta, delta, coverage)
        assert [e.text[:10] for e in kept] == ['{"stdout":', "B方案执行"]

    def test_unflagged_session_keeps_everything(self):
        delta = [_event("assistant", "普通会话的结论段")]
        coverage = {}
        assert host_events._drop_distiller_echo(delta, delta, coverage) == delta
        assert "_distiller_session" not in coverage

    def test_event_stream_with_job_op_sets_sticky_flag(self):
        # transcript 兜底路径：事件流里出现 job 工具调用即粘性标记
        events = [_event("tool", "x", "mcp__knowbase__memory_job_claim"),
                  _event("assistant", "回复文本")]
        coverage = {}
        kept = host_events._drop_distiller_echo([events[1]], events, coverage)
        assert kept == [] and coverage.get("_distiller_session") is True

    def test_sticky_flag_survives_when_stream_no_longer_shows_op(self):
        coverage = {"_distiller_session": True}
        events = [_event("assistant", "后续轮次即使没有 job 调用也不再入队")]
        assert host_events._drop_distiller_echo(list(events), events, coverage) == []


class TestHandleMarksDistiller:
    def test_memory_job_tool_call_marks_session(self, isolated_repo):
        conn = index.connect(isolated_repo)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        conn.execute("INSERT INTO context_sessions(host_profile,session_id,scope,coverage_json,updated_at) "
                     "VALUES('zcode','session_distiller','knowbase','{}',?)", (now,))
        conn.commit()
        conn.close()

        host_events.handle({"session_id": "session_distiller", "tool_name": "mcp__knowbase__memory_job_claim",
                            "tool_input": {}}, event="tool-before", style="zcode")

        conn = index.connect(isolated_repo)
        row = conn.execute("SELECT coverage_json FROM context_sessions WHERE session_id='session_distiller'").fetchone()
        conn.close()
        assert json.loads(row[0]).get("_distiller_session") is True

    def test_regular_memory_tool_does_not_mark(self, isolated_repo):
        conn = index.connect(isolated_repo)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        conn.execute("INSERT INTO context_sessions(host_profile,session_id,scope,coverage_json,updated_at) "
                     "VALUES('zcode','session_plain','knowbase','{}',?)", (now,))
        conn.commit()
        conn.close()

        host_events.handle({"session_id": "session_plain", "tool_name": "mcp__knowbase__memory_search",
                            "tool_input": {"query": "x"}}, event="tool-before", style="zcode")

        conn = index.connect(isolated_repo)
        row = conn.execute("SELECT coverage_json FROM context_sessions WHERE session_id='session_plain'").fetchone()
        conn.close()
        assert not json.loads(row[0]).get("_distiller_session")


class TestCaptureIncrementWithDistiller:
    def test_flagged_session_tool_material_still_captured(self, isolated_repo):
        conn = index.connect(isolated_repo)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        conn.execute("INSERT INTO context_sessions(host_profile,session_id,scope,coverage_json,updated_at) "
                     "VALUES('zcode','session_cap','knowbase',?,?)",
                     (json.dumps({"_distiller_session": True}), now))
        conn.execute("INSERT INTO context_events(host_profile,session_id,event_id,trigger,payload_hash,"
                     "status,created_at,observation_json) VALUES('zcode','session_cap','event-1','observe','event-1',"
                     "'executed',?,?)",
                     (now, json.dumps({"hook_event": "tool-after",
                                       "payload": {"tool_name": "Bash", "tool_input": {"command": "echo fix"},
                                                   "toolResponse": {"stdout": "修复后的工具证据"}}})))
        conn.commit()
        conn.close()

        result = host_events.capture_increment(isolated_repo, "session_cap", "", "knowbase", "zcode")
        assert result.get("capture_status") == "queued"
        from knowbase import materials
        conn = index.connect(isolated_repo)
        payload = materials.payload_for_job(conn, result["job_ids"][0])
        conn.close()
        assert any("修复后的工具证据" in s["text"] for s in payload["segments"])
        assert all(s.get("role") != "assistant" for s in payload["segments"])
