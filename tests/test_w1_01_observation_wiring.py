"""W1-1：observation.ingest 接线（Hook 工具事件 + MCP 记忆调用）。

覆盖：Hook tool-before/tool-after 成对落 native_events（配对计数 1）；
同事件重放幂等；scope 未解析时仍记录；MCP search/read/context/use 各记一条；
无 session_id 的 search 不记；自调用（memory_*）不经 Hook 路径重复记。
"""
import json

from knowbase import config, index, store, writer
from knowbase.adapters import host_events
from knowbase.knowledge_lifecycle import observation
from knowbase.migrations import apply_all
from knowbase.mutations import _compute_revision
from knowbase.server import context_impl, read_impl, search_impl, use_impl

BODY = ("## 结论\nHook 与 MCP 调用进观测台账。\n## 解决的问题\nx。\n"
        "## 适用条件\ny。\n## 不适用条件\nz。\n## 可执行动作\n运行 scripts/a.py。\n"
        "## 关键证据\nPR。\n## 验证情况\nv。\n## 未知与待确认\n无。\n")


def enable(repo, project):
    cfg = json.loads(config.CONFIG_PATH.read_text())
    cfg["inflight"] = {"enabled": True, "mode": "assisted", "checkpoint_work_ops": 100}
    cfg["scope_map"] = {str(project): "orders"}
    config.CONFIG_PATH.write_text(json.dumps(cfg))


def _native_rows(conn, host, session):
    return conn.execute(
        "SELECT native_key,event_kind,tool_name,tool_call_id FROM native_events "
        "WHERE host_profile=? AND session_id=? ORDER BY rowid",
        (host, session)).fetchall()


def test_hook_tool_events_land_in_observation(memory_repo, tmp_path):
    enable(memory_repo, tmp_path)
    transcript = tmp_path / "conversation.jsonl"
    transcript.write_text("", encoding="utf-8")
    base = {"session_id": "obs-s1", "tool_name": "Bash", "tool_use_id": "call-1",
            "cwd": str(tmp_path), "transcript_path": str(transcript),
            "tool_input": {"command": "pytest tests/test_x.py"},
            "tool_response": {"exit_code": 0}}
    before = host_events.handle(dict(base), event="tool-before", style="zcode")
    assert not before.get("error"), before
    after = host_events.handle(dict(base), event="tool-after", style="zcode")
    assert not after.get("error"), after
    conn = index.connect(memory_repo)
    apply_all(conn)
    rows = _native_rows(conn, "zcode", "obs-s1")
    assert [r[1] for r in rows] == ["tool_call", "tool_result"]
    assert {r[3] for r in rows} == {"call-1"}
    assert {r[2] for r in rows} == {"Bash"}
    # 同事件重放幂等：不再新增行。
    host_events.handle(dict(base), event="tool-before", style="zcode")
    assert len(_native_rows(conn, "zcode", "obs-s1")) == 2
    # run 汇总：无 gap（tool_call/tool_result 已配对）。
    run = conn.execute("SELECT run_id FROM task_runs WHERE host_profile=? AND session_id=?",
                       ("zcode", "obs-s1")).fetchone()
    assert run
    summary = observation.run_summary(conn, run[0])
    assert summary["gap_flags"] == []
    conn.close()


def test_hook_tool_event_uses_explicit_ancestor_scope(memory_repo, tmp_path):
    enable(memory_repo, tmp_path)
    (tmp_path / "unmapped").mkdir()
    result = host_events.handle(
        {"session_id": "obs-unresolved", "tool_name": "Read",
         "tool_use_id": "call-u1", "cwd": str(tmp_path / "unmapped"),
         "tool_input": {"file_path": "x.py"}},
        event="tool-before", style="zcode")
    assert result.get("scope") == "orders"
    conn = index.connect(memory_repo)
    assert len(_native_rows(conn, "zcode", "obs-unresolved")) == 1
    conn.close()


def test_hook_self_calls_are_not_double_recorded(memory_repo, tmp_path):
    enable(memory_repo, tmp_path)
    result = host_events.handle(
        {"tool_name": "mcp__knowbase__memory_search", "session_id": "s"},
        event="tool-after", style="zcode")
    assert result["status"] == "skipped_self"
    conn = index.connect(memory_repo)
    assert conn.execute("SELECT COUNT(*) FROM native_events").fetchone()[0] == 0
    conn.close()


def test_mcp_memory_calls_land_in_observation(memory_repo):
    # 统一生命周期：项目内新增需内容依据（provenance）才直接发布 active
    # 并进入默认检索；否则落 staging 不出现在 candidates。
    card = writer.create(memory_repo, "pitfall", "检索观测卡", BODY, scope="orders",
                         provenance="PR-114")
    assert not card.get("error"), card
    mid = card["entity_id"]
    conn = index.connect(memory_repo)
    apply_all(conn)
    host = config.agent_name()
    # search：带 session 记一条；不带 session 不记。
    search_impl("检索观测卡", scope="orders", session_id="obs-m1")
    search_impl("检索观测卡", scope="orders")
    # context + read + use（同一会话）。
    ctx = json.loads(context_impl(session_id="obs-m1", event_id="lookup", scope="orders",
                                  current_goal="验证观测接线", host_profile="native-test",
                                  topics=[{"topic_id": "t1", "queries": ["检索观测卡"],
                                           "status": "active"}]))
    assert not ctx.get("error"), ctx
    card = next(c for c in ctx["candidates"] if c["id"] == mid)
    text = read_impl(mid, session_id="obs-m1", event_id="read-1",
                     context_event_id="lookup", host_profile="native-test")
    assert "read_use_id=" in text, text
    meta, body, _ = store.load(memory_repo, mid)
    use = json.loads(use_impl(session_id="obs-m1", event_id="use-1",
                              context_event_id="lookup", id=mid,
                              content_revision=card["content_revision"],
                              state="referenced", host_profile="native-test"))
    assert not use.get("error"), use
    rows = _native_rows(conn, host, "obs-m1")
    kinds = {(r[1], r[2]) for r in rows}
    assert ("tool_call", "memory_search") in kinds
    assert ("tool_call", "memory_context") in kinds
    assert ("tool_call", "memory_read") in kinds
    assert ("tool_call", "memory_use") in kinds
    # search 只记了一条（无 session 的那次不记）。
    assert sum(1 for r in rows if r[2] == "memory_search") == 1
    # 同调用重放幂等。
    search_impl("检索观测卡", scope="orders", session_id="obs-m1")
    assert sum(1 for r in _native_rows(conn, host, "obs-m1")
               if r[2] == "memory_search") == 1
    # 同一会话的 Hook 与 MCP 事件落在同一个 run。
    run = conn.execute("SELECT run_id FROM task_runs WHERE host_profile=? AND session_id=?",
                       (host, "obs-m1")).fetchone()
    assert run
    summary = observation.run_summary(conn, run[0])
    assert summary["identity_basis"] == "local_segment"
    conn.close()
