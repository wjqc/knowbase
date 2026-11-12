"""宿主原生事件 → V2 观测台账的接线适配器（W1-1）。

设计决策（任务书 W1-1(c)）：不新增 `observation.batch` MCP 工具。
Agent 额外调一个工具来记录观测，违背零额外负担原则；改为在 Hook 与 MCP
impl 内自动记录。调用方负责脱敏：Hook 用 extraction.sanitizer（与
context_events 同口径）；MCP 只记工具名与结构化参数，不记正文。

project_key：git remote URL（跨机可比）；未配置 remote 时 repo:unknown。

MCP 记忆调用是单边观测（无配对的 tool_result），会体现在 task_runs 的
unpaired_tool_calls 计数中；配对质量只对 Hook 工具调用有意义。
观测失败不阻断主流程（调用方 try/except 包裹，遥测同款 best-effort 语义）。
"""
import hashlib
import os
import re
import sqlite3
from pathlib import Path

from .. import config, consumption, delivery, index, materials, scope_binding
from ..extraction.sanitizer import sanitize_value
from . import contracts, observation


def project_key_for_repo(repo, cfg=None) -> str:
    """仓库身份：git remote URL；未配置时 repo:unknown。"""
    cfg = cfg or config.load_config()
    url = str((((cfg.get("git") or {}).get("remote") or {}).get("url") or "")).strip()
    return contracts.project_key_from_repo(url)


def _digest(payload) -> str:
    return hashlib.sha256(
        contracts.canonical_json(payload).encode("utf-8")).hexdigest()[:16]


def observe_hook_tool_event(conn, *, host_profile, session_id, project_key,
                            event, tool_name, tool_call_id, payload,
                            native_task_key="", native_run_key="") -> dict:
    """Hook 的一次工具事件 → observation.ingest。

    tool-before → tool_call；tool-after/tool-failure → tool_result。
    其他事件（user-prompt/stop/recover/before-compact）不是工具观测，跳过。
    native_key 稳定：同事件重放幂等（ingest 按 native_key + payload hash 去重）。
    """
    if not host_profile or not session_id:
        return {"status": "skipped", "reason": "no_host_or_session"}
    kind = {"tool-before": "tool_call",
            "tool-after": "tool_result",
            "tool-failure": "tool_result"}.get(event)
    if not kind:
        return {"status": "skipped", "reason": "not_a_tool_event"}
    call_id = str(tool_call_id or "")
    native_key = (f"hook:{event}:{call_id}" if call_id
                  else f"hook:{event}:digest:{_digest(payload or {})}")
    return observation.ingest(
        conn, host_profile, session_id,
        [{"native_key": native_key, "kind": kind, "tool_call_id": call_id,
          "tool_name": str(tool_name or "")[:128], "payload": payload or {}}],
        project_key=project_key, native_task_key=native_task_key,
        native_run_key=native_run_key or session_id)


def observe_mcp_call(conn, *, host_profile, session_id, project_key, tool_name,
                     event_id="", payload=None, native_task_key="",
                     native_run_key="") -> dict:
    """MCP 记忆工具调用 → observation.ingest（kind=tool_call，单边观测）。

    native_key：Agent 传的 event_id 优先（同一调用重放幂等）；无 event_id 时
    用参数摘要作 key（同参数重放幂等，不同参数不冲突）。
    无 session_id 时不记（不伪造会话身份）。
    """
    if not host_profile or not session_id:
        return {"status": "skipped", "reason": "no_host_or_session"}
    payload = payload or {}
    key = str(event_id or "")
    native_key = (f"mcp:{tool_name}:{key}" if key
                  else f"mcp:{tool_name}:digest:{_digest(payload)}")
    return observation.ingest(
        conn, host_profile, session_id,
        [{"native_key": native_key, "kind": "tool_call",
          "tool_call_id": key, "tool_name": str(tool_name or "")[:128],
          "payload": payload}],
        project_key=project_key, native_task_key=native_task_key,
        native_run_key=native_run_key or session_id,
        identity_basis="mcp_claim" if native_task_key else "local_segment")


def host_session(data):
    return str(data.get("session_id") or data.get("sessionId") or
               os.environ.get("ZCODE_SESSION_ID") or os.environ.get("CLAUDE_SESSION_ID") or "")[:512]


def workspace_fingerprint(path) -> str:
    return "workspace:" + materials.digest(str(Path(path).expanduser().resolve()))


def start_host_task(conn, *, host, session, data, project, scope, prompt):
    if not (session and project and scope and prompt):
        return ""
    identity = scope_binding.git_identity(project) or {}
    remote = identity.get("remote")
    project_key = contracts.project_key_from_repo(
        ("git:" + materials.digest(re.sub(r"(://)[^/@]+@", r"\1", remote))) if remote else
        ("local:" + materials.digest(identity.get("toplevel") or str(Path(project).resolve()))))
    native = data.get("task_id") or data.get("taskId")
    turn = data.get("turn_id") or data.get("turnId") or data.get("message_id") or data.get("messageId")
    task = str(native)[:256] if native else "prompt-" + materials.digest([project_key, turn or prompt])[:24]
    origin = str(data.get("run_id") or data.get("runId") or turn or f"{session}:{task}")[:512]
    result = observation.ingest(conn, host, session, [{"native_key": f"task-start:{task}:{origin}",
        "kind": "task_start", "payload": {}}], project_key=project_key,
        native_task_key=task, native_run_key=origin,
        environment_fingerprint=workspace_fingerprint(identity.get("toplevel") or project),
        identity_basis="native" if native else "host_prompt")
    conn.execute("INSERT INTO host_task_anchors VALUES(?,?,?,?,?,?) ON CONFLICT(host_profile,session_id) "
                 "DO UPDATE SET task_id=excluded.task_id,project_key=excluded.project_key,"
                 "run_id=excluded.run_id,scope=excluded.scope",
                 (host, session, task, project_key, result["run_id"], scope))
    conn.commit()
    return task


def _response_text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return _response_text(value.get("text") or value.get("content") or value.get("result") or value.get("output"))
    if isinstance(value, list):
        return "\n".join(_response_text(item) for item in value)
    return ""


def _observe_memory_result(repo, conn, *, host, session, task, scope, tool, args, response):
    text = _response_text(response)
    if tool == "memory_search":
        for token in re.findall(r'delivery_id="(DEL-[a-f0-9]{24})"', text):
            delivered = delivery.by_id(conn, token)
            if (delivered and delivered["context_event_id"].startswith("mcp:")
                    and delivery.same_host(delivered["host_profile"], host)
                    and delivered["scope"] in (scope, "global")):
                conn.execute("UPDATE retrieval_deliveries SET host_profile=?,session_id=?,task_id=? "
                             "WHERE delivery_id=? AND session_id IN ('',?) AND task_id IN ('',?)",
                             (host, session, task, token, session, task))
        conn.commit()
    elif tool == "memory_read":
        mid = args.get("id") or ""
        header = next((line for line in text.splitlines()[:3] if line.startswith(f"[{mid}] ")), "")
        revision = re.search(r" ｜ content_revision=([a-f0-9]+)(?: ｜|$)", header)
        if not mid or not revision:
            return
        token = args.get("delivery_id")
        if token:
            item = delivery.by_id(conn, token)
            matches = [item] if item and (item["host_profile"], item["session_id"], item["task_id"],
                item["memory_id"], item["content_revision"]) == (host, session, task, mid, revision[1]) else []
        else:
            matches = delivery.for_task(conn, host=host, session=session, task=task,
                                        memory_id=mid, revision=revision[1])
        if len(matches) == 1:
            consumption.record_delivered_read(repo, matches[0])


def observe_host_event(repo, data, *, host, event):
    session = host_session(data)
    if not session:
        return
    conn = sqlite3.connect(index.db_path(repo).resolve().as_uri() + "?mode=rw", uri=True, timeout=.05)
    try:
        anchor = conn.execute("SELECT task_id,project_key,run_id,scope FROM host_task_anchors "
                              "WHERE host_profile=? AND session_id=?", (host, session)).fetchone()
        if not anchor:
            return
        task, project, run_id, scope = anchor
        native_task = data.get("task_id") or data.get("taskId")
        if native_task and native_task != task:
            return True
        summary = observation.run_summary(conn, run_id)
        turn = data.get("turn_id") or data.get("turnId")
        if summary["identity_basis"] == "host_prompt" and turn:
            if task != "prompt-" + materials.digest([project, turn])[:24]:
                return True
        origin = summary["origin_run_key"].removeprefix(f"run:{project}:")
        identity = {"project_key": project, "native_task_key": task, "native_run_key": origin,
                    "identity_basis": summary["identity_basis"]}
        if event == "stop":
            observation.ingest(conn, host, session, [{"kind": "task_end",
                "native_key": f"task-stop:{run_id}", "payload": {"completion": "host_stopped"}}], **identity)
            return True
        tool = str(data.get("tool_name") or data.get("toolName") or "")
        args = data.get("tool_input") or data.get("toolInput") or {}
        response = data.get("tool_response") or data.get("tool_result") or data.get("toolResponse") or data.get("toolResult") or {}
        status = response if isinstance(response, dict) else {}
        failed = (event == "tool-failure" or data.get("error") or status.get("error")
                  or status.get("is_error") or status.get("isError") or status.get("interrupted")
                  or status.get("timedOut") or status.get("cancelled"))
        call_id = str(data.get("tool_use_id") or data.get("tool_call_id") or
                      data.get("toolCallId") or data.get("toolUseId") or "")
        if not isinstance(args, dict):
            args = {}
        if call_id and event == "tool-after":
            prior = conn.execute("SELECT run_id FROM native_events WHERE host_profile=? AND session_id=? "
                                 "AND native_key=?", (host, session, f"hook:tool-before:{call_id}")).fetchone()
            if prior and prior[0] != run_id:
                return True
        memory_tool = re.search(r"(?:^|[_.:])(memory_(?:search|read))$", tool)
        if memory_tool and event == "tool-after" and not failed:
            _observe_memory_result(repo, conn, host=host, session=session, task=task, scope=scope,
                                   tool=memory_tool[1], args=args, response=response)
        if not call_id or re.search(r"(?:^|[_.:])memory_", tool):
            return True
        if event == "tool-before":
            payload = sanitize_value({key: str(args[key])[:2048] for key in
                                      ("command", "file_path", "filePath", "path", "delivery_id") if key in args})
        else:
            exit_code = status.get("exit_code", status.get("exitCode", data.get("exit_code")))
            payload = {"exit_code": exit_code, "succeeded": not failed and exit_code in (0, "0", None)}
        observation.ingest(conn, host, session, [{"kind": "tool_call" if event == "tool-before" else "tool_result",
            "native_key": f"hook:{event}:{call_id}", "tool_call_id": call_id, "tool_name": tool,
            "payload": payload}], **identity)
        return True
    finally:
        conn.close()
