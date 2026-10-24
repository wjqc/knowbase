"""工具事件正式 CLI、增量采集和 scope/递归边界。真实宿主消费另验。"""

import json
import os
import subprocess
import sys

from knowbase import config, index, materials
from knowbase.adapters import host_events


def enable(repo, project):
    cfg = json.loads(config.CONFIG_PATH.read_text())
    cfg["inflight"] = {"enabled": True, "mode": "assisted", "checkpoint_work_ops": 1}
    cfg["scope_map"] = {str(project): "orders"}
    config.CONFIG_PATH.write_text(json.dumps(cfg))


def event(project, transcript, call="one"):
    return {"session_id": "host-s1", "tool_name": "Read", "tool_use_id": call,
            "cwd": str(project), "transcript_path": str(transcript),
            "tool_input": {"file_path": "orders.py"}, "tool_response": {"exit_code": 0}}


def test_tool_after_queues_increment_without_new_user_message(memory_repo, tmp_path):
    enable(memory_repo, tmp_path)
    transcript = tmp_path / "conversation.jsonl"
    transcript.write_text(json.dumps({"role": "user", "content": "业务规则：已结算订单不能撤回。"}, ensure_ascii=False) + "\n")
    first = host_events.handle(event(tmp_path, transcript), event="tool-after", style="zcode")
    assert first["job_ids"]
    first_job = first["job_ids"][0]
    with transcript.open("a") as f:
        f.write(json.dumps({"role": "assistant", "content": "阶段二：订单撤回需校验审批角色。"}, ensure_ascii=False) + "\n")
    second = host_events.handle(event(tmp_path, transcript, "two"), event="tool-after", style="zcode")
    conn = index.connect(memory_repo)
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 2
    jobs = [r[0] for r in conn.execute("SELECT job_id FROM jobs WHERE job_id!=?", (first_job,))]
    payload = materials.payload_for_job(conn, jobs[0])
    assert len(payload["checkpoint"]["events"]) == 1
    assert "审批角色" in payload["segments"][0]["text"]
    assert "memory_job_claim" not in host_events.render(second)
    conn.close()


def test_existing_save_does_not_hide_new_transcript_segment(memory_repo, tmp_path):
    enable(memory_repo, tmp_path)
    transcript = tmp_path / "conversation.jsonl"
    transcript.write_text(json.dumps({"type": "tool_use", "name": "memory_save", "input": {}}) + "\n" +
                          json.dumps({"role": "user", "text": "用户纠正：撤回还需审批角色授权。"}, ensure_ascii=False) + "\n")
    result = host_events.handle(event(tmp_path, transcript), event="stop", style="zcode")
    assert result["job_ids"]


def test_knowbase_self_calls_are_excluded(memory_repo, tmp_path):
    enable(memory_repo, tmp_path)
    result = host_events.handle({"tool_name": "mcp__knowbase__memory_context", "session_id": "s"},
                              event="tool-after", style="zcode")
    assert result["status"] == "skipped_self"
    conn = index.connect(memory_repo)
    assert conn.execute("SELECT COUNT(*) FROM context_events").fetchone()[0] == 0
    conn.close()


def test_unknown_project_does_not_fall_back_to_global(memory_repo, tmp_path):
    enable(memory_repo, tmp_path)
    result = host_events.handle({"session_id": "unknown", "tool_name": "Read", "cwd": str(tmp_path / "unmapped")},
                              event="tool-before", style="zcode")
    assert result["error"] == "SCOPE_UNRESOLVED"
    conn = index.connect(memory_repo)
    assert conn.execute("SELECT status FROM context_events").fetchone()[0] == "scope_unresolved"
    conn.close()


def test_cli_outputs_only_strict_additional_context_json(memory_repo, tmp_path):
    enable(memory_repo, tmp_path)
    env = {**os.environ, "KNOWBASE_CONFIG": str(config.CONFIG_PATH)}
    result = subprocess.run([sys.executable, "-m", "knowbase", "hook", "tool-before", "--style", "zcode"],
        input=json.dumps({"session_id": "cli-s1", "tool_name": "Read", "cwd": str(tmp_path), "tool_use_id": "read-1"}),
        capture_output=True, text=True, env=env, timeout=10)
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert set(output) == {"additionalContext"}
    assert "memory_context" in output["additionalContext"]


def test_new_error_retains_existing_task_topics(memory_repo, tmp_path):
    enable(memory_repo, tmp_path)
    from knowbase import context
    context.execute(memory_repo, session_id="host-s1", event_id="plan", scope="orders", host_profile="zcode",
                    task_revision="v1", topics=[{"topic_id": "permissions", "queries": ["订单 权限"], "status": "active"}])
    failure = event(tmp_path, tmp_path / "missing", "fail-1")
    failure["tool_response"] = {"exit_code": 1, "error": "OrderStateConflict"}
    host_events.handle(failure, event="tool-failure", style="zcode")
    conn = index.connect(memory_repo)
    topics = json.loads(conn.execute("SELECT topics_json FROM context_sessions WHERE session_id='host-s1'").fetchone()[0])
    assert "permissions" in [t["topic_id"] for t in topics]
    assert any(t["topic_id"].startswith("failure:") for t in topics)
    conn.close()


def test_repeated_error_with_new_call_id_does_not_repeat_search(memory_repo, tmp_path):
    enable(memory_repo, tmp_path)
    first = event(tmp_path, tmp_path / "missing", "failure-a")
    first["tool_response"] = {"exit_code": 1, "error": "OrderStateConflict"}
    initial = host_events.handle(first, event="tool-failure", style="zcode")
    second = {**first, "tool_use_id": "failure-b"}
    repeated = host_events.handle(second, event="tool-failure", style="zcode")
    assert initial["budget_used"]["queries"] == 1
    assert repeated["budget_used"]["queries"] == 0


def test_claude_tool_hook_uses_event_specific_context_envelope(memory_repo, tmp_path):
    enable(memory_repo, tmp_path)
    result = subprocess.run([sys.executable, "-m", "knowbase", "hook", "tool-before", "--style", "claude"],
        input=json.dumps({"session_id": "claude-s1", "tool_name": "Read", "cwd": str(tmp_path), "tool_use_id": "r1"}),
        capture_output=True, text=True, timeout=10, env={**os.environ, "KNOWBASE_CONFIG": str(config.CONFIG_PATH)})
    assert result.returncode == 0
    output = json.loads(result.stdout)
    assert set(output) == {"hookSpecificOutput"}
    assert output["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    assert "memory_context" in output["hookSpecificOutput"]["additionalContext"]


def test_observed_payload_survives_absent_transcript_and_stop_does_not_recur(memory_repo, tmp_path):
    enable(memory_repo, tmp_path)
    input_data = {"session_id": "observed", "prompt": "业务规则：已结算订单禁止撤回。", "cwd": str(tmp_path)}
    host_events.handle(input_data, event="user-prompt", style="zcode")
    result = host_events.handle({"session_id": "observed", "cwd": str(tmp_path)}, event="stop", style="zcode")
    assert result["job_ids"]
    conn = index.connect(memory_repo)
    first_count = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    payload = materials.payload_for_job(conn, result["job_ids"][0])
    assert payload["coverage_scope"] == "observed_host_events"
    assert "禁止撤回" in payload["segments"][0]["text"]
    conn.close()
    host_events.handle({"session_id": "observed", "cwd": str(tmp_path), "response": "已处理完毕"}, event="stop", style="zcode")
    conn = index.connect(memory_repo)
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == first_count
    conn.close()


def test_capture_failure_is_recorded_and_not_reported_as_no_hit(memory_repo, tmp_path):
    enable(memory_repo, tmp_path)
    malformed = tmp_path / "bad.jsonl"
    malformed.write_text('{"role":"user","text":"rule"}\ninvalid-json\n')
    result = host_events.handle(event(tmp_path, malformed), event="tool-after", style="zcode")
    assert result["error"] == "MATERIAL_PARSE_FAILED"
    conn = index.connect(memory_repo)
    assert conn.execute("SELECT status FROM context_events").fetchone()[0] == "material_parse_failed"
    conn.close()


def test_unchanged_pending_job_does_not_repeat_context_on_every_tool(memory_repo, tmp_path):
    enable(memory_repo, tmp_path)
    transcript = tmp_path / "pending.jsonl"
    transcript.write_text(json.dumps({"role": "user", "text": "订单结算后禁止撤回。"}, ensure_ascii=False) + "\n")
    first = host_events.handle(event(tmp_path, transcript, "a"), event="tool-after", style="zcode")
    assert first["job_ids"] and first["notify_job_ids"] == []
    repeated = host_events.handle(event(tmp_path, transcript, "b"), event="tool-before", style="zcode")
    assert repeated["job_ids"]
    assert repeated["notify_job_ids"] == []
    assert "增量提炼待办" not in host_events.render(repeated)


def test_hook_query_plan_uses_same_real_session_as_mcp(memory_repo, tmp_path):
    enable(memory_repo, tmp_path)
    first = host_events.handle(event(tmp_path, tmp_path / "unused", "plan-before"), event="tool-before", style="zcode")
    assert first["needs_query_plan"]
    bridge = first["planning_context"]
    assert bridge["session_id"] == "host-s1" and bridge["host_profile"] == "zcode"
    from knowbase.server import context_impl
    planned = json.loads(context_impl(**bridge, current_goal="订单撤回校验",
        topics=[{"topic_id": "withdraw", "queries": ["订单 撤回"], "status": "active"}]))
    assert not planned.get("error"), planned
    next_event = host_events.handle(event(tmp_path, tmp_path / "unused", "next-before"), event="tool-before", style="zcode")
    assert not next_event["needs_query_plan"]
    conn = index.connect(memory_repo)
    assert conn.execute("SELECT COUNT(*) FROM context_sessions").fetchone()[0] == 1
    assert "withdraw" in conn.execute("SELECT topics_json FROM context_sessions").fetchone()[0]
    conn.close()


def test_stage_capture_uses_persisted_source_without_recopying_long_input(memory_repo, tmp_path):
    enable(memory_repo, tmp_path)
    # 先保存长用户输入；不再向 memory_capture 重传该文本。
    text = "阶段需求正文\n" + "背景说明。" * 3000 + "\n尾部规则：已结算订单禁止撤回。"
    host_events.handle({"session_id": "source-session", "prompt": text, "cwd": str(tmp_path)},
                       event="user-prompt", style="zcode")
    from knowbase.server import capture_impl
    captured = capture_impl(session_id="source-session", scope="orders", host_profile="zcode")
    assert "已入队" in captured and "已持久化观察" in captured
    conn = index.connect(memory_repo)
    job = conn.execute("SELECT job_id FROM jobs").fetchone()[0]
    payload = materials.payload_for_job(conn, job)
    assert "".join(s["text"] for s in payload["segments"]) == text
    conn.close()
    repeated = capture_impl(session_id="source-session", scope="orders", host_profile="zcode")
    assert "无新增" in repeated


def test_persisted_source_capture_rejects_different_scope(memory_repo, tmp_path):
    enable(memory_repo, tmp_path)
    host_events.handle({"session_id": "source-session", "prompt": "订单规则", "cwd": str(tmp_path)},
                       event="user-prompt", style="zcode")
    from knowbase.server import capture_impl
    assert "SCOPE_MISMATCH" in capture_impl(session_id="source-session", scope="other", host_profile="zcode")
    conn = index.connect(memory_repo)
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
    conn.close()


def test_unmapped_project_hint_can_bind_session_without_global_fallback(memory_repo, tmp_path):
    enable(memory_repo, tmp_path)
    unknown = tmp_path / "new-project"
    request = {"session_id": "new-scope", "tool_name": "Read", "cwd": str(unknown), "tool_use_id": "r1"}
    first = host_events.handle(request, event="tool-before", style="zcode")
    assert first["error"] == "SCOPE_UNRESOLVED"
    assert "memory_context" in host_events.render(first)
    assert "new-scope" in host_events.render(first)
    from knowbase.server import context_impl
    binding = json.loads(context_impl(**first["planning_context"], scope="new-project",
        current_goal="本项目校验", topics=[{"topic_id":"checks","queries":["校验"],"status":"active"}]))
    assert binding["scope"] == "new-project" and not binding.get("error")
    later = host_events.handle({**request, "tool_use_id":"r2"}, event="tool-before", style="zcode")
    assert later["scope"] == "new-project" and not later.get("error")


def test_query_observation_pilot_does_not_automatically_enqueue(memory_repo, tmp_path):
    enable(memory_repo, tmp_path)
    cfg = json.loads(config.CONFIG_PATH.read_text())
    cfg["inflight"]["capture_enabled"] = False
    config.CONFIG_PATH.write_text(json.dumps(cfg))
    result = host_events.handle(event(tmp_path, tmp_path / "missing"), event="tool-after", style="zcode")
    assert not result.get("error")
    conn = index.connect(memory_repo)
    assert conn.execute("SELECT COUNT(*) FROM context_events WHERE observation_json!='{}'").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
    conn.close()
