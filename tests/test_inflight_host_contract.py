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
    assert "memory_job_claim" in host_events.render(second)
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
