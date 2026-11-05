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

from .. import config
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
        native_run_key=native_run_key or session_id)
