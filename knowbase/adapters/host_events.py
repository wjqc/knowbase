"""宿主工具事件适配；真实能力认证与处理器协议测试分开。"""

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from .. import config, context, index, jobs, materials
from . import normalizer


def _host(style):
    return config.load_config()["inflight"].get("host_profile") or style


def _sanitize_observation(value):
    from ..extraction.sanitizer import sanitize
    if isinstance(value, str):
        return sanitize(value)
    if isinstance(value, dict):
        return {k: "[REDACTED:credential]" if re.search(r"password|authorization|api[_-]?key|access[_-]?token", str(k), re.I)
                else _sanitize_observation(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize_observation(v) for v in value]
    return value


def _observed_events(conn, session_id, scope, host):
    events = []
    for event_id, raw in conn.execute("SELECT event_id,observation_json FROM context_events "
                                     "WHERE host_profile=? AND session_id=? AND observation_json!='{}' ORDER BY rowid",
                                     (host, session_id)):
        observation = json.loads(raw)
        data = observation.get("payload", {})
        if observation.get("hook_event") in ("stop", "before-compact", "recover"):
            continue  # Stop 本身不成为下一次 capture 的材料，避免自我循环。
        if observation.get("hook_event") == "user-prompt":
            text = data.get("prompt") or data.get("prompt_text") or data.get("user_prompt") or data.get("input") or ""
            role = "user"
        else:
            text, role = json.dumps(observation, ensure_ascii=False), "tool"
        events.append(normalizer.normalize_event({"id": event_id, "role": role, "text": text},
                                                default_session_id=session_id, default_scope=scope))
    return events


def capture_increment(repo, session_id, transcript_path, scope, host):
    """只从显式宿主 transcript 引用采集未入队增量，不扫描聊天目录。"""
    path = Path(transcript_path or "")
    if path.is_file() and path.stat().st_size > 10 * 1024 * 1024:
        return {"error": "MATERIAL_TOO_LARGE", "needs_input": True}
    events = (normalizer.parse_transcript_auto(str(path), default_session_id=session_id, default_scope=scope, strict=True)
              if path.is_file() else None)
    conn = index.connect(repo)
    try:
        row = conn.execute("SELECT coverage_json FROM context_sessions WHERE host_profile=? AND session_id=?",
                           (host, session_id)).fetchone()
        coverage = json.loads(row[0]) if row else {}
        checkpoint = coverage.get("_hook_capture", {})
        source_kind = "transcript" if events is not None else "observations"
        if events is None:
            events = _observed_events(conn, session_id, scope, host)
        cursor = checkpoint.get("cursor", 0) if checkpoint.get("source_kind", "transcript") == source_kind else 0
        prefix_hash = materials.digest([e.text for e in events[:cursor]])
        if cursor > len(events) or (cursor and prefix_hash != checkpoint.get("prefix_hash")):
            events = _observed_events(conn, session_id, scope, host)
            source_kind, cursor = "observations", 0
        delta = events[cursor:]
        if not delta:
            return {"job_ids": [], "capture_status": "no_delta"}
        backlog = conn.execute("SELECT COUNT(*) FROM jobs WHERE state NOT IN "
                               "('succeeded','no_candidate','needs_review','failed')").fetchone()[0]
        if backlog >= config.load_config()["inflight"]["max_pending_jobs"]:
            return {"error": "BACKLOG_FULL"}
        payload = materials.capture_payload(delta, scope=scope, session_id=session_id)
        payload.update(host_profile=host, capture_range={"source_kind": source_kind, "start": cursor, "end": len(events)},
                       coverage_scope="observed_host_events" if source_kind == "observations" else "provided_transcript")
        if not payload["segments"]:
            return {"job_ids": [], "capture_status": "no_delta"}
        queued = jobs.enqueue(conn, jobs.KIND_CAPTURE, scope, payload,
                              idempotency_key=f"capture:{host}:{session_id}:{scope}:{cursor}:{payload['input_version']}")
        if queued.get("error"):
            return queued
        if queued["created"] or queued.get("state") in ("queued", "preparing"):
            materials.register_manifest(conn, queued["job_id"], payload)
            jobs.set_preparing(conn, queued["job_id"])
            jobs.set_awaiting(conn, queued["job_id"], len(payload["segments"]))
        # 这里只推进已持久入队游标；settled 仍由物化服务维护。
        event_count = conn.execute("SELECT COUNT(*) FROM context_events WHERE host_profile=? AND session_id=? "
                                   "AND trigger IN ('observe','new_failure')", (host, session_id)).fetchone()[0]
        coverage["_hook_capture"] = {"cursor": len(events), "prefix_hash": materials.digest([e.text for e in events]),
                                      "event_count": event_count, "source_kind": source_kind}
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        conn.execute("INSERT INTO context_sessions(host_profile,session_id,scope,coverage_json,last_checkpoint_at,updated_at,observed_cursor) "
                     "VALUES(?,?,?,?,?,?,?) ON CONFLICT(host_profile,session_id) DO UPDATE SET "
                     "coverage_json=excluded.coverage_json,last_checkpoint_at=excluded.last_checkpoint_at,"
                     "updated_at=excluded.updated_at,observed_cursor=excluded.observed_cursor",
                     (host, session_id, scope, json.dumps(coverage), now, now, f"{source_kind}:{len(events)}"))
        conn.commit()
        return {"job_ids": [queued["job_id"]], "capture_status": "queued", "captured_events": len(delta)}
    finally:
        conn.close()


def handle(data: dict, *, event: str, style: str) -> dict:
    cfg = config.load_config()
    if not cfg["inflight"]["enabled"]:
        return {"status": "disabled"}
    session = str(data.get("session_id") or data.get("sessionId") or os.environ.get("CLAUDE_SESSION_ID")
                  or os.environ.get("ZCODE_SESSION_ID") or "")
    if not session:
        return {"status": "failed", "error": "SESSION_ID_MISSING"}
    host = _host(style)
    tool = str(data.get("tool_name") or data.get("toolName") or "")
    tool_input = data.get("tool_input") or data.get("toolInput") or {}
    observation = _sanitize_observation({"hook_event": event, "payload": data})
    if len(json.dumps(observation, ensure_ascii=False).encode("utf-8")) > 10 * 1024 * 1024:
        return {"status": "failed", "error": "OBSERVATION_TOO_LARGE"}
    command = tool_input.get("command", "") if isinstance(tool_input, dict) else ""
    self_call = bool(re.search(r"(?:^|[_:.])memory_", tool)) or "-m knowbase" in str(command)
    if self_call:
        return {"status": "skipped_self"}
    repo = config.repo_path(cfg)
    if not repo.exists():
        return {"status": "failed", "error": "REPOSITORY_MISSING"}
    project = data.get("cwd") or data.get("project_dir") or os.environ.get("ZCODE_PROJECT_DIR") or os.environ.get("CLAUDE_PROJECT_DIR")
    conn = index.connect(repo)
    try:
        row = conn.execute("SELECT scope,topics_json,last_checkpoint_at,coverage_json FROM context_sessions "
                           "WHERE host_profile=? AND session_id=?", (host, session)).fetchone()
        scope = row[0] if row else ""
        if project:
            path = Path(project).expanduser().resolve()
            mapped = config.mapped_scope(str(project), cfg) or ""
            if not mapped:
                known = conn.execute("SELECT 1 FROM meta WHERE scope=? LIMIT 1", (path.name,)).fetchone()
                mapped = path.name if known else ""
            if mapped:
                scope = mapped
            elif not row or (row[0] == "global" and not json.loads(row[1]) and not json.loads(row[3])):
                scope = ""
        if not scope and not project:
            scope = "global"
        last_at = row[2] if row else None
        count = conn.execute("SELECT COUNT(*) FROM context_events WHERE host_profile=? AND session_id=? "
                             "AND trigger IN ('observe','new_failure')", (host, session)).fetchone()[0]
        captured_count = json.loads(row[3]).get("_hook_capture", {}).get("event_count", 0) if row else 0
        if not last_at:
            last_at = conn.execute("SELECT MIN(created_at) FROM context_events WHERE host_profile=? AND session_id=?",
                                   (host, session)).fetchone()[0]
    finally:
        conn.close()
    if not scope:
        # context 内核统一记录无法解析，而不是退化成全库。
        return context.execute(repo, session_id=session,
            event_id=materials.digest({"event": event, "input": data}), stage="observe", scope="",
            host_profile=host, current_goal="", automatic=True, project_path=str(project or ""), observation=observation)
    failed = event == "tool-failure"
    response = data.get("tool_response") or data.get("tool_result") or {}
    if isinstance(response, dict):
        failed = failed or response.get("is_error", False) or response.get("exit_code", 0) not in (0, None)
    call_id = data.get("tool_use_id") or data.get("tool_call_id") or data.get("toolCallId")
    identity = str(call_id) if call_id else materials.digest(data)
    stage = ("new_failure" if failed else "subtask_start" if event == "tool-before" else
             "checkpoint" if event == "stop" else "before_compact" if event == "before-compact" else
             "recover" if event == "recover" else "observe")
    refs = []
    topics = None
    if failed:
        from ..extraction.sanitizer import sanitize
        error_value = (response.get("error") or response.get("stderr") or response.get("output")
                       if isinstance(response, dict) else response)
        failure_text = sanitize(str(data.get("error") or error_value or response))
        failure_text = re.sub(r"\x1b\[[0-9;]*m|\b\d{4}-\d\d-\d\dT[\d:.+-]+Z?|0x[0-9a-fA-F]+", "<volatile>", failure_text)
        refs = [{"tool_call_id": identity, "error_hash": materials.digest(failure_text)}]
        queries = re.findall(r"(?:[A-Za-z][\w.:-]{2,}|[\u4e00-\u9fff]{3,16})", failure_text)[:8]
        topics = [{"topic_id": "failure:" + materials.digest(failure_text), "status": "active",
                   "queries": [" ".join(queries)] if queries else [tool + " 执行失败"]}]
    result = context.execute(repo, session_id=session, event_id=f"{event}:{identity}", scope=scope,
                             stage=stage, topics=topics, evidence_refs=refs, host_profile=host, automatic=True,
                             observation=observation, project_path=str(project or ""))
    if result.get("error"):
        return result
    due = event in ("stop", "before-compact") or event in ("tool-after", "tool-failure") and count - captured_count + 1 >= cfg["inflight"]["checkpoint_work_ops"]
    if last_at:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(last_at)).total_seconds()
        due = due or age >= cfg["inflight"]["checkpoint_minutes"] * 60
    transcript = data.get("transcript_path") or data.get("transcriptPath")
    if due:
        captured = capture_increment(repo, session, transcript, scope, host)
        pending = result.get("job_ids", [])
        result.update(captured)
        result["job_ids"] = list(dict.fromkeys(pending + captured.get("job_ids", [])))[:cfg["inflight"]["max_batches_per_opportunity"]]
    prompt = data.get("prompt") or data.get("prompt_text") or ""
    result["needs_query_plan"] = (event == "tool-before" and not row) or (event == "user-prompt" and len(prompt) > 2000)
    return result


def render(result):
    if result.get("status") in {"disabled", "skipped_self"}:
        return ""
    if result.get("error"):
        return "[knowbase] 执行中知识处理未完成：" + str(result["error"]) + "；请核对 scope/待办，不把失败当作无知识。"
    lines = []
    if result.get("needs_query_plan"):
        lines.append("[knowbase] 请用 memory_context 提交当前子任务的主题与短查询；执行中进入新子任务/新错误仍需补查，无需等待用户消息。")
    if result.get("candidates"):
        lines.append("[knowbase 执行中候选] 先核对适用条件，再按需 memory_read；命中不代表已经验证：")
        for c in result["candidates"]:
            lines.append(f"- [{c['id']}] {c['summary']}；适用：{c['applicable_when']}；{c['confidence']}")
    if result.get("job_ids"):
        lines.append("[knowbase 增量提炼待办] " + ", ".join(result["job_ids"]) +
            "。请 memory_job_claim(protocol_version=2) 领取，memory_job_material 续页，核对技术经验及业务规则，查重后提交；无价值片段给明确理由。")
    return "\n".join(lines)
