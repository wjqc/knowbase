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
    from ..extraction.sanitizer import sanitize_value
    return sanitize_value(value)


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


def _enqueued_text_hashes(conn, host, session_id):
    """同一宿主会话此前已入队片段的文本哈希集合。

    会话标识可能含下划线，不能用 LIKE 匹配（通配符歧义），用字典序区间定位
    该会话的全部 capture 任务，再取其片段哈希；input_version/source_kind 漂移
    触发的整段重排在这里被内容级去重拦截。
    """
    prefix = f"capture:{host}:{session_id}:"
    rows = conn.execute(
        "SELECT js.text_hash FROM job_segments js JOIN jobs j ON js.job_id=j.job_id "
        "WHERE j.kind=? AND j.idempotency_key >= ? AND j.idempotency_key < ?",
        (jobs.KIND_CAPTURE, prefix, prefix[:-1] + ";"))
    return {r[0] for r in rows}


def _is_self_event(event) -> bool:
    """knowbase 自身工具调用不作为提炼材料（观察层已跳过，transcript 路径在此过滤）。

    归一化事件的 text 会把原始观察 JSON 作为转义字符串内嵌，工具名字段因此可能是
    \"tool_name\" 形态，正则对引号前可选反斜杠兼容。
    """
    if getattr(event, "tool_name", "") and re.search(r"(?:^|[_:.])memory_", str(event.tool_name)):
        return True
    text = event.text or ""
    for name in re.findall(r'\\?"(?:tool_name|toolName)\\?"\s*:\s*\\?"([^"\\]*)', text):
        if re.search(r"(?:^|[_:.])memory_", name):
            return True
    return "-m knowbase" in text


def _is_distiller_op(event) -> bool:
    """会话是否在处理提炼作业本身（memory_job_* 调用）。

    判据比 _is_self_event 窄：memory_search/save 等常规读写不算，只有
    claim/material/submit/renew/status 序列才标志会话在跑提炼循环。
    只认结构化工具名（tool_name 字段或转义 JSON 内的 tool_name 键），
    不做裸子串匹配——催办提示等正文里也会出现 memory_job_ 字样，不能算数。
    """
    pattern = r"(?:^|[_:.])memory_job_"
    if getattr(event, "tool_name", "") and re.search(pattern, str(event.tool_name)):
        return True
    text = event.text or ""
    for name in re.findall(r'\\?"(?:tool_name|toolName)\\?"\s*:\s*\\?"([^"\\]*)', text):
        if re.search(pattern, name):
            return True
    return False


def _drop_distiller_echo(delta: list, events: list, coverage: dict) -> list:
    """提炼会话的 assistant 回复段不入队（自回声源头过滤）。

    背景：处理提炼作业的会话，每轮回复被 Stop 捕获成 1 段材料，形成
    "处理→催办→回复→再入队"死循环。会话粘性标记 _distiller_session
    （handle() 在 memory_job_* 调用时落标，transcript 路径兜底扫描），
    标记后该会话 assistant 角色段全部丢弃，工具载荷照常采集。
    """
    distiller = bool(coverage.get("_distiller_session")) or any(_is_distiller_op(e) for e in events)
    if not distiller:
        return delta
    coverage["_distiller_session"] = True
    return [e for e in delta if getattr(e, "role", "") != "assistant"]


def _mark_distiller_session(repo, host: str, session_id: str) -> None:
    """memory_job_* 实调即粘性标记提炼会话；标记失败静默，不影响宿主调用。"""
    try:
        conn = index.connect(repo)
        try:
            row = conn.execute("SELECT coverage_json FROM context_sessions WHERE host_profile=? AND session_id=?",
                               (host, session_id)).fetchone()
            if not row:
                return
            coverage = json.loads(row[0]) if row[0] else {}
            if coverage.get("_distiller_session"):
                return
            coverage["_distiller_session"] = True
            conn.execute("UPDATE context_sessions SET coverage_json=?,updated_at=? WHERE host_profile=? AND session_id=?",
                         (json.dumps(coverage), datetime.now(timezone.utc).isoformat(timespec="seconds"),
                          host, session_id))
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def _save_capture_checkpoint(conn, host, session_id, scope, coverage, events, source_kind):
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


def capture_increment(repo, session_id, transcript_path, scope, host):
    """只从显式宿主 transcript 引用采集未入队增量，不扫描聊天目录。"""
    path = Path(transcript_path or "")
    if path.is_file() and path.stat().st_size > 10 * 1024 * 1024:
        return {"error": "MATERIAL_TOO_LARGE", "needs_input": True}
    events = (normalizer.parse_transcript_auto(str(path), default_session_id=session_id, default_scope=scope, strict=True)
              if path.is_file() else None)
    conn = index.connect(repo)
    try:
        row = conn.execute("SELECT coverage_json,scope FROM context_sessions WHERE host_profile=? AND session_id=?",
                           (host, session_id)).fetchone()
        if not row:
            return {"error": "SESSION_CONTEXT_MISSING", "needs_input": True}
        if row[1] != scope:
            return {"error": "SCOPE_MISMATCH"}
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
        delta = [e for e in events[cursor:] if not _is_self_event(e)]
        delta = _drop_distiller_echo(delta, events, coverage)
        if not delta:
            # 增量全被过滤（自身调用/提炼会话回声）：推进游标避免每次钩子重算。
            _save_capture_checkpoint(conn, host, session_id, scope, coverage, events, source_kind)
            return {"job_ids": [], "capture_status": "no_delta"}
        backlog = conn.execute("SELECT COUNT(*) FROM jobs WHERE state NOT IN "
                               "('succeeded','no_candidate','needs_review','failed')").fetchone()[0]
        if backlog >= config.load_config()["inflight"]["max_pending_jobs"]:
            return {"error": "BACKLOG_FULL"}
        payload = materials.capture_payload(delta, scope=scope, session_id=session_id)
        seen = _enqueued_text_hashes(conn, host, session_id)
        if seen:
            payload["segments"] = [s for s in payload["segments"] if s["text_hash"] not in seen]
            payload["total_segments"] = len(payload["segments"])
        if not payload["segments"]:
            # 全部为重复/自身调用：推进游标避免每次钩子重算，不产生新任务。
            _save_capture_checkpoint(conn, host, session_id, scope, coverage, events, source_kind)
            return {"job_ids": [], "capture_status": "no_delta"}
        payload.update(host_profile=host, capture_range={"source_kind": source_kind, "start": cursor, "end": len(events)},
                       coverage_scope="observed_host_events" if source_kind == "observations" else "provided_transcript")
        queued = jobs.enqueue(conn, jobs.KIND_CAPTURE, scope, payload,
                              idempotency_key=f"capture:{host}:{session_id}:{scope}:{cursor}:{payload['input_version']}")
        if queued.get("error"):
            return queued
        if queued["created"] or queued.get("state") in ("queued", "preparing"):
            materials.register_manifest(conn, queued["job_id"], payload)
            jobs.set_preparing(conn, queued["job_id"])
            jobs.set_awaiting(conn, queued["job_id"], len(payload["segments"]))
        # 这里只推进已持久入队游标；settled 仍由物化服务维护。
        _save_capture_checkpoint(conn, host, session_id, scope, coverage, events, source_kind)
        return {"job_ids": [queued["job_id"]], "capture_status": "queued", "captured_events": len(delta)}
    finally:
        conn.close()


def pending_notifications(conn, job_ids, notices: dict, *, force: bool = False) -> list:
    """计算待推送的提炼待办：只推活跃态作业。

    终态（succeeded/no_candidate/needs_review/failed）与 blocked/缺失的作业不再催办，
    并清掉其历史签名，避免旧签名在作业复活（重开）前持续遮蔽新通知。
    """
    notifiable = {jobs.STATE_QUEUED, jobs.STATE_PREPARING, jobs.STATE_AWAITING,
                  jobs.STATE_CLAIMED, jobs.STATE_VALIDATING, jobs.STATE_RETRY_WAIT}
    notifications = []
    for job_id in job_ids:
        job = jobs.status(conn, job_id)
        if job is None or job["state"] not in notifiable:
            notices.pop(job_id, None)
            continue
        signature = f"{job['state']}:{job['cursor']}:{job['attempt']}"
        if force or notices.get(job_id) != signature:
            notifications.append(job_id)
            notices[job_id] = signature
    return notifications


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
        # 提炼作业工具实调：粘性标记该会话为提炼会话，其 assistant 回复不再入队
        if re.search(r"(?:^|[_:.])memory_job_", tool):
            _mark_distiller_session(config.repo_path(cfg), host, session)
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
            elif not row or context.provisional_global(row[0], row[1], row[3]):
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
        known_scopes = [r[0] for r in conn.execute("SELECT DISTINCT scope FROM meta WHERE scope!='global' ORDER BY scope LIMIT 20")]
        cutoff = (datetime.now(timezone.utc).timestamp() - 60)
        recent_scope_errors = [r[0] for r in conn.execute("SELECT created_at FROM context_events WHERE host_profile=? "
                               "AND session_id=? AND status='scope_unresolved' ORDER BY rowid DESC LIMIT 1", (host, session))]
        scope_notice = not recent_scope_errors or datetime.fromisoformat(recent_scope_errors[0]).timestamp() < cutoff
    finally:
        conn.close()
    if not scope:
        # context 内核统一记录无法解析，而不是退化成全库。
        result = context.execute(repo, session_id=session,
            event_id=materials.digest({"event": event, "input": data}), stage="observe", scope="",
            host_profile=host, current_goal="", automatic=True, project_path=str(project or ""), observation=observation)
        result.update(scope_binding_required=True, scope_notice=scope_notice, project_path=str(project or ""),
            known_scopes=known_scopes, planning_context={"session_id":session,"host_profile":host,
                "event_id":"scope-plan:"+materials.digest(data),"stage":"task_plan"})
        return result
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
    if result.get("host_handled"):
        return result
    due = event in ("stop", "before-compact") or event in ("tool-after", "tool-failure") and count - captured_count + 1 >= cfg["inflight"]["checkpoint_work_ops"]
    if last_at:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(last_at)).total_seconds()
        due = due or age >= cfg["inflight"]["checkpoint_minutes"] * 60
    transcript = data.get("transcript_path") or data.get("transcriptPath")
    if due and cfg["inflight"]["capture_enabled"]:
        try:
            captured = capture_increment(repo, session, transcript, scope, host)
        except (OSError, ValueError, TypeError) as exc:
            captured = {"error": "MATERIAL_PARSE_FAILED", "capture_status": "failed", "failure_class": type(exc).__name__}
        pending = result.get("job_ids", [])
        result.update(captured)
        result["job_ids"] = list(dict.fromkeys(pending + captured.get("job_ids", [])))[:cfg["inflight"]["max_batches_per_opportunity"]]
    prompt = data.get("prompt") or data.get("prompt_text") or ""
    result["needs_query_plan"] = (event == "tool-before" and (not row or not json.loads(row[1]))) or (event == "user-prompt" and len(prompt) > 2000)
    result["planning_context"] = {"session_id": session, "host_profile": host, "scope": scope,
                                  "event_id": "agent-plan:" + identity, "stage": "task_plan"}
    result["host_handled"] = True
    conn = index.connect(repo)
    try:
        current = conn.execute("SELECT coverage_json FROM context_sessions WHERE host_profile=? AND session_id=?",
                               (host, session)).fetchone()
        coverage = json.loads(current[0]) if current else {}
        if result["needs_query_plan"]:
            last = coverage.get("_hook_plan_notice", "")
            now = datetime.now(timezone.utc)
            if last and event != "user-prompt" and (now - datetime.fromisoformat(last)).total_seconds() < 60:
                result["needs_query_plan"] = False
            else:
                coverage["_hook_plan_notice"] = now.isoformat(timespec="seconds")
        notices = coverage.get("_hook_notices", {})
        result["notify_job_ids"] = pending_notifications(
            conn, result.get("job_ids", []), notices, force=event in ("stop", "recover"))
        coverage["_hook_notices"] = notices
        conn.execute("UPDATE context_sessions SET coverage_json=? WHERE host_profile=? AND session_id=?",
                     (json.dumps(coverage), host, session))
        status = str(result["error"]).lower() if result.get("error") else result.get("retrieval_status", "executed")
        conn.execute("UPDATE context_events SET result_json=?,status=?,reason=?,job_ids_json=? "
                     "WHERE host_profile=? AND session_id=? AND event_id=?",
                     (json.dumps(result, ensure_ascii=False), status, result.get("error", ""),
                      json.dumps(result.get("job_ids", [])), host, session, f"{event}:{identity}"))
        conn.commit()
    finally:
        conn.close()
    return result


def render(result):
    if result.get("status") in {"disabled", "skipped_self"}:
        return ""
    if result.get("scope_binding_required"):
        if not result.get("scope_notice", True):
            return ""
        return ("[knowbase] 当前项目范围未确定，尚未检索，未退回全库。请根据当前项目选原有 scope（未建库的新项目用明确项目标识），"
                "调用 memory_context 并补 scope/current_goal/topics，沿用会话身份："
                + json.dumps(result["planning_context"], ensure_ascii=False)
                + "。项目路径：" + result.get("project_path", "unknown")
                + "；已有 scope 标签（部分）：" + ", ".join(result.get("known_scopes", []))
                + "。项目归属不明确时保持未检索，不查询其他项目内容。")
    if result.get("error"):
        return "[knowbase] 执行中知识处理未完成：" + str(result["error"]) + "；请核对 scope/待办，不把失败当作无知识。"
    lines = []
    if result.get("needs_query_plan"):
        identity = json.dumps(result.get("planning_context", {}), ensure_ascii=False)
        lines.append("[knowbase] 请调用 memory_context，沿用以下实际会话身份：" + identity +
                     "，并补齐当前任务的 current_goal/topics（主题与具体短查询）。后续阶段沿用同一 session_id/host_profile，"
                     "使用新的 event_id；执行中进入新子任务/新错误仍需补查，无需等待用户消息。")
    if result.get("candidates"):
        lines.append("[knowbase 执行中候选] 先核对适用条件，再按需 memory_read；命中不代表已经验证：")
        for c in result["candidates"]:
            lines.append(f"- [{c['id']}] {c['summary']}；适用：{c['applicable_when']}；{c['confidence']}")
    notifications = result.get("notify_job_ids", result.get("job_ids", []))
    if notifications:
        lines.append("[knowbase 增量提炼待办] " + ", ".join(notifications) +
            "。请 memory_job_claim(protocol_version=2) 领取，memory_job_material 续页，核对技术经验及业务规则，查重后提交；无价值片段给明确理由。")
    return "\n".join(lines)
