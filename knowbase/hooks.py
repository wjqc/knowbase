"""Hook 集成：轻量提示 / 阶段检索 / 静默增量入队。

职责划分（设计 6.1）：Hook = 触发器，LLM = 相关性裁判，MCP = 持久化。
核心函数供测试直接调用；cmd_* 包装层从 stdin 读 Claude Code hook JSON、
只注入有预算的相关提示，Stop 不输出阻断。要求轻量导入（不引 mcp/server）。
"""

import json
import os
import re
import sys
import tempfile
from pathlib import Path

from . import config, index, sources, store

MIN_PROMPT_LEN = 2
MAX_HITS = 2
MEMORY_WRITE_TOOLS = ("memory_save", "memory_update", "memory_feedback")
MEMORY_READ_TOOLS = ("memory_read",)
FEEDBACK_TOOL = "memory_feedback"
WORK_TOOLS = ("Edit", "Write", "NotebookEdit", "Bash")


# ---------- 1. 会话开始：注入任务规则 ----------

def session_start() -> str:
    return (
        "[knowbase 经验库] 按需辅助当前任务：\n"
        "1. 具体项目/新子任务/新错误先用 memory_context 分阶段检索，topics格式为"
        '[{"topic_id":"本阶段","queries":["具体技术词"],"status":"active"}]；简单检索用memory_search，传正确scope。'
        "只对相关候选memory_read，沿用递送提供的delivery_id；采用与验证由后台记录可观察事实。\n"
        "2. 需要报告知识纠错、过时或实际帮助时可用memory_feedback，"
        "通常只传已有use_id、明确outcome和实际task_result；没有可核对结果就不填。\n"
        "3. 日常任务用完整的业务答复直接结束。Hook静默采集入队，不阻断主任务；"
        "不要主动保存或提炼，不等待采集，不为失败补写、重试或报告回执。"
        "除非用户明确询问知识来源，最终答复只报告工作结果，不转述读卡、递送标识、反馈或后台验证状态。\n"
        "4. 只有用户明确要求保存或维护经验时，才用memory_save、memory_update或memory_job_*；"
        "业务规则须有出处。不为补反馈延长会话。"
    )


# ---------- 2. 用户提示词：统一检索并注入命中 ----------


def _infer_scope(conn, project_dir=None) -> str | None:
    from .scope_binding import resolve_project

    return resolve_project(conn, project_dir)["scope"]


def has_project_context() -> bool:
    return bool(os.environ.get("CLAUDE_PROJECT_DIR") or os.environ.get("ZCODE_PROJECT_DIR"))


def _tier_hits(conn, hits, min_conf):
    """Phase 2 tiered injection. Returns (kept, dropped_once).

    kept is a list of (hit, kind): kind 'full' injects the normal line,
    kind 'summary' injects a pointer-only line. Verified cards inject in
    full; once cards inject a summary pointer only when their current
    revision carries adopted helpful evidence, otherwise they are dropped;
    staging/non-active cards never inject in any mode. Modes outside the
    verified family keep their previous full-line behaviour.
    """
    from . import evolution
    kept, dropped_once = [], 0
    once_cap = 1 if min_conf == "verified_plus_once_gated" else None
    once_summaries = 0
    for h in hits:
        if h.get("staging") or (h.get("status") is not None and h["status"] != "active"):
            continue
        conf = h.get("confidence")
        if min_conf in ("verified", "verified_plus_once_gated"):
            if conf == "verified":
                kept.append((h, "full"))
            elif conf == "once":
                if once_cap is not None and once_summaries >= once_cap:
                    dropped_once += 1
                    continue
                ev = evolution.evidence_for_revision(conn, h["id"], h.get("content_revision", ""))
                if ev.get("helpful_tasks", 0) >= 1:
                    kept.append((h, "summary"))
                    once_summaries += 1
                else:
                    dropped_once += 1
            # any other confidence is dropped in the verified family
        else:
            kept.append((h, "full"))
    return kept, dropped_once


def user_prompt(prompt: str, project_dir=None, delivery_info=None, host_data=None) -> str:
    """对用户输入做本地检索（自动推断当前项目 scope），命中则返回注入文本。

    Phase 1 查询卫生（02 号设计§5）：先过意图门控与保守蒸馏再检索；
    长文/粘贴文档跳过检索与注入；技术标识原文保留。auto_search 永为本地检索。
    """
    hooks_cfg = config.load_config().get("hooks", {}) or {}
    if not hooks_cfg.get("enabled", True):
        return ""
    prompt = (prompt or "").strip()
    if not prompt or (len(prompt) < MIN_PROMPT_LEN and not host_data):
        return ""
    from uuid import uuid4
    from . import delivery, query_hygiene, scope_binding
    hy = query_hygiene.prepare(prompt)  # V1 safety limits cannot be disabled by config.
    rp = config.repo_path()
    if not rp.exists():
        return ""
    conn = index.connect(rp)
    try:
        resolution = scope_binding.resolve_project(conn, project_dir)
        inferred = resolution["scope"]
        scope = inferred or "global"
        host = (delivery_info or {}).get("host_profile") or config.agent_name()
        sid = (delivery_info or {}).get("session_id") or ""
        task = ""
        if host_data and sid and inferred:
            try:
                from .knowledge_lifecycle.observe import start_host_task
                task = start_host_task(conn, host=host, session=sid, data=host_data,
                                       project=project_dir, scope=inferred, prompt=prompt)
            except Exception:
                conn.rollback()
        if len(prompt) < MIN_PROMPT_LEN:
            return ""
        from .build_identity import build_id
        diagnostics = {"scope_resolved": bool(inferred), "scope_source": resolution["source"],
                       "scope_reason": resolution["reason"], "host_profile": host,
                       "runtime_build": build_id()}
        if hy["action"] == "skip":
            # 跳过也留痕（query 存前 200 字符防日志膨胀），便于统计跳过原因分布。
            index.record_search(conn, prompt[:200], "", [], tool="auto_search",
                                query_class=f"skipped:{hy['reason']}", raw_len=len(prompt),
                                retrieval={**diagnostics, "origin": "hook_prompt", "level": "not_executed"})
            return ""
        search_id = str(uuid4())
        hits = index.search(conn, hy["query"], scope=scope, limit=100, origin="hook_prompt", diagnostics=diagnostics)
        min_conf = hooks_cfg.get("inject_min_confidence", "verified")
        # 矛盾知识只供主动调查，不自动建议采用。
        hits = [h for h in hits if not any(r.get("type") == "contradicts"
                for r in (store.load(rp, h["id"])[0] or {}).get("relations", []))]
        from .mutations import _compute_revision
        version_safe = []
        for hit in hits:
            meta,body,_=store.load(rp,hit["id"])
            if meta and (not meta.get("verified_content_revision") or
                         meta.get("verified_content_revision")==_compute_revision(meta,body)):
                hit["citations"] = sources.card_citations(meta, 1)
                hit["source_changes"] = sources.card_source_changes(conn, meta, 5)
                version_safe.append(hit)
        kept, dropped_once = _tier_hits(conn, version_safe, min_conf)
        header = "[knowbase 自动检索] 本地候选；先核对项目、版本与证据，再按需 memory_read："
        budget = max(0, min(900, int(hooks_cfg.get("inject_max_chars", 900))))
        lines, delivered = [header], []
        for hit, kind in kept[:MAX_HITS]:
            if kind == "summary":
                line = f"- [{hit['id']}] {hit['title']}（once·未验证，仅指针：用 memory_read 读原文核对后再用 · scope={hit['scope']}）"
            else:
                label="once·未验证" if hit["confidence"]=="once" else hit["confidence"]
                line = f"- [{hit['id']}] {hit['title']}（{label}·{hit['status']} · scope={hit['scope']}）"
            if sid and hit.get("content_revision"):
                token = delivery.delivery_id_for(search_id, hit["id"], hit["content_revision"])
                line += f' · memory_read(id="{hit["id"]}", delivery_id="{token}")'
            if hit["source_changes"]:
                line += " · " + sources.revalidation_notice(hit["source_changes"])
            if hit["citations"]:
                line += " · 出处 " + hit["citations"][0]
            if len("\n".join(lines + [line])) > budget:
                continue
            lines.append(line)
            delivered.append(hit)
        text = "\n".join(lines) if delivered else ""
        kind_by_id = {h["id"]: kind for h, kind in kept}
        delivered_tiers = {}
        for h in delivered:
            kind = kind_by_id[h["id"]]
            tier = "once_summary" if kind == "summary" else ("verified_full" if h.get("confidence") == "verified" else "full")
            delivered_tiers.setdefault(tier, []).append(h["id"])
        diagnostics.update(delivered_chars=len(text), delivered_count=len(delivered),
                           delivered_tiers=delivered_tiers, dropped_once=dropped_once)
        search_id=index.record_search(conn, hy["query"], scope, delivered, tool="auto_search",
                            query_class=hy["query_class"], raw_len=len(prompt), retrieval=diagnostics, search_id=search_id)
        if delivery_info is not None:
            delivery_ids = []
            for rank, hit in enumerate(delivered, 1):
                delivery_ids.append(delivery.record_delivery(
                    conn, search_id=search_id, host_profile=host, session_id=sid,
                    context_event_id=f"hook:{search_id}", task_id=task, task_revision="",
                    scope=scope, memory_id=hit["id"],
                    content_revision=hit.get("content_revision") or "", rank=rank))
            conn.commit()
            delivery_info["search_id"]=search_id
            delivery_info["delivery_ids"]=delivery_ids
            delivery_info["tiers"]=delivered_tiers
            delivery_info["dropped_once"]=dropped_once
        return text
    finally:
        conn.close()



# ---------- 3. 会话结束：静默采集，不要求为沉淀/反馈延长任务 ----------

def _transcript_stats(path: str) -> dict:
    """扫描会话 transcript：返回各类工具操作计数（记忆类为 0/1 标志，work_ops 为次数）。"""
    stats = {"memory_write": 0, "memory_read": 0, "feedback": 0, "work_ops": 0}
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            for line in f:
                if '"tool_use"' not in line:
                    continue
                if any(f'"{t}"' in line for t in MEMORY_WRITE_TOOLS):
                    stats["memory_write"] = 1
                if any(f'"{t}"' in line for t in MEMORY_READ_TOOLS):
                    stats["memory_read"] = 1
                if f'"{FEEDBACK_TOOL}"' in line:
                    stats["feedback"] = 1
                if any(f'"{t}"' in line for t in WORK_TOOLS):
                    stats["work_ops"] += 1
    except OSError:
        pass
    return stats


def _safe_session_token(session_id: str) -> str:
    """Windows 文件名禁止 : * ? 等字符；同一 session_id 必须稳定映射到同一路径。"""
    return re.sub(r"[^A-Za-z0-9._-]", "_", session_id)[:120]


def _flag_path(session_id: str) -> Path:
    base = os.environ.get("KNOWBASE_HOOK_STATE") or tempfile.gettempdir()
    return Path(base) / f"knowbase-hook-{_safe_session_token(session_id)}.flag"


def _mid_capture_flag_path(session_id: str) -> Path:
    """中途 capture 的 flag 文件路径（每个会话最多触发一次）。"""
    base = os.environ.get("KNOWBASE_HOOK_STATE") or tempfile.gettempdir()
    return Path(base) / f"knowbase-mid-capture-{_safe_session_token(session_id)}.flag"


def stop_event(session_id: str, transcript_path: str, *, allow_enqueue: bool = True) -> str:
    """Legacy hooks may queue material; knowledge maintenance never blocks Stop."""
    cfg = config.load_config()
    if not cfg.get("hooks", {}).get("enabled", True):
        return ""
    t = _transcript_stats(transcript_path)
    if allow_enqueue and t["work_ops"] and not t["memory_write"]:
        _try_enqueue_capture(session_id, transcript_path, cfg)
    return ""


def _try_enqueue_capture(session_id: str, transcript_path: str, cfg: dict) -> str:
    """旧收尾/中途入口也复用完整材料 manifest，不再截断或建第二套任务。"""
    from . import materials
    from .adapters import normalizer
    rp = config.repo_path(cfg)
    if not rp.exists():
        return ""
    conn = index.connect(rp)
    try:
        scope = _infer_scope(conn)
        if has_project_context() and not scope:
            return ""
        scope = scope or "global"
        events = normalizer.parse_transcript_auto(transcript_path, default_session_id=session_id,
                                                  default_scope=scope, strict=True)
        if not events:
            return ""
        from .hook_policy import filter_capture
        from .adapters.host_events import _is_self_event, _drop_distiller_echo
        events = _drop_distiller_echo([e for e in events if not _is_self_event(e)], events, {})
        events, _ = filter_capture(events)
        payload = materials.capture_payload(events, scope=scope, session_id=session_id)
        if not payload["segments"]:
            return ""
        from . import jobs
        result = jobs.enqueue(conn, jobs.KIND_CAPTURE, scope, payload,
                              idempotency_key=f"capture:legacy-hook:{session_id}:{scope}:{payload['input_version']}")
        if result.get("error"):
            return ""
        if result["created"] or result.get("state") in ("queued", "preparing"):
            materials.register_manifest(conn, result["job_id"], payload)
            jobs.set_preparing(conn, result["job_id"])
            jobs.set_awaiting(conn, result["job_id"], len(payload["segments"]))
        return result["job_id"]
    except (OSError, ValueError, TypeError):
        return ""  # 提醒仍会保留；不能以坏来源创建空成功任务。
    finally:
        conn.close()


# ---------- stdin/stdout 包装层 ----------

def _utf8_stdio():
    """宿主按 UTF-8 收发 Hook 输入输出；Windows 管道默认走本地代码页（如 cp936），
    中文 payload 会乱码甚至解码失败（异常被吞掉后 Hook 静默失效），入口处统一对齐。"""
    for stream, extra in ((sys.stdin, {}), (sys.stdout, {"newline": "\n"})):
        try:
            stream.reconfigure(encoding="utf-8", **extra)
        except (AttributeError, ValueError, OSError):
            pass


def _read_stdin() -> dict:
    _utf8_stdio()
    try:
        data = json.load(sys.stdin)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _first(d: dict, *keys) -> str:
    for k in keys:
        if d.get(k):
            return str(d[k])
    return ""


def cmd_session_start(style: str = "claude"):
    os.environ.setdefault("KNOWBASE_AGENT_NAME", style)
    data = _read_stdin()
    if not config.load_config().get("hooks", {}).get("enabled", True):
        return
    text = session_start()
    if config.load_config()["inflight"]["enabled"] and (data.get("session_id") or data.get("sessionId")):
        from .adapters import host_events
        result = host_events.handle(data, event="recover", style=style, defer_capture=True)
        text += "\n" + host_events.render_foreground(result, data, event="recover", style=style)
    _emit(text, style)


def cmd_user_prompt(style: str = "claude"):
    os.environ.setdefault("KNOWBASE_AGENT_NAME", style)
    data = _read_stdin()
    prompt = _first(data, "prompt", "prompt_text", "user_prompt", "input")
    # M3-2: 中途检查点触发（不阻断，异步入队）
    from .knowledge_lifecycle.observe import host_session
    sid = host_session(data)
    transcript = _first(data, "transcript_path", "transcriptPath", "transcript")
    cfg = config.load_config()
    if (sid and transcript and not cfg["inflight"]["enabled"] and cfg["inflight"]["capture_enabled"]
            and cfg["hooks"].get("enabled", True) and cfg["hooks"].get("mid_session_capture", False)
            and not _mid_capture_flag_path(sid).exists()):
        _defer_legacy_capture(data, cfg, style, mid_session=True)
    delivery_info={"session_id": sid or "", "host_profile": cfg["inflight"].get("host_profile") or style}
    project = (_first(data, "cwd", "project_dir", "projectDir") or os.environ.get("ZCODE_PROJECT_DIR")
               or os.environ.get("CLAUDE_PROJECT_DIR"))
    text = user_prompt(prompt, project, delivery_info, host_data=data)
    if cfg["inflight"]["enabled"]:
        from .adapters import host_events
        observation = host_events.handle(data, event="user-prompt", style=style, defer_capture=True)
        text = host_events.render_foreground(observation, data, event="user-prompt", style=style, prefix=text)
    from .retrieval_metrics import delivery
    delivery(config.repo_path(),delivery_info.get("search_id"),text,
             tiers=delivery_info.get("tiers"), dropped_once=delivery_info.get("dropped_once",0))
    _emit(text, style)


def _defer_legacy_capture(data, cfg, style, *, mid_session=False):
    from .adapters.checkpoint_queue import enqueue
    sid = _first(data, "session_id", "sessionId") or os.environ.get("ZCODE_SESSION_ID") or os.environ.get("CLAUDE_SESSION_ID")
    transcript = _first(data, "transcript_path", "transcript", "transcriptPath")
    if not sid or not transcript:
        return {"error": "CHECKPOINT_REFERENCE_MISSING"}
    project = (_first(data, "cwd", "project_dir") or os.environ.get("ZCODE_PROJECT_DIR")
               or os.environ.get("CLAUDE_PROJECT_DIR") or "")
    return enqueue(config.repo_path(cfg), sid, transcript, "", style, project=project,
                   legacy=True, mid_session=mid_session)


def cmd_stop(style: str = "claude"):
    """Always exit quietly: durable capture is independent of foreground continuation."""
    os.environ.setdefault("KNOWBASE_AGENT_NAME", style)
    data = _read_stdin()
    try:
        cfg = config.load_config()
        if not cfg["hooks"].get("enabled", True):
            return
        if not cfg["inflight"]["enabled"]:
            from .knowledge_lifecycle.observe import observe_host_event
            try:
                observe_host_event(config.repo_path(cfg), data,
                                   host=cfg["inflight"].get("host_profile") or style, event="stop")
            except Exception:
                pass
        if cfg["inflight"]["enabled"]:
            from .adapters import host_events
            host_events.handle(data, event="stop", style=style, defer_capture=True)
        elif cfg["inflight"]["capture_enabled"]:
            _defer_legacy_capture(data, cfg, style)
    except Exception:
        # Stop 输出属于宿主会话；采集失败不能进入主任务的收尾。
        return


def cmd_tool_event(style: str = "claude", event: str = "tool-after"):
    os.environ.setdefault("KNOWBASE_AGENT_NAME", style)
    from .adapters import host_events
    data = _read_stdin()
    try:
        result = host_events.handle(data, event=event, style=style, defer_capture=True)
        text = host_events.render_foreground(result, data, event=event, style=style)
    except Exception as exc:
        print("[knowbase] hook incomplete: " + type(exc).__name__, file=sys.stderr)
        return
    if not text:
        return
    if style in ("claude", "trae", "codex"):
        external = {"tool-before": "PreToolUse", "tool-after": "PostToolUse", "tool-failure": "PostToolUseFailure"}[event]
        print(json.dumps({"hookSpecificOutput": {"hookEventName": external, "additionalContext": text}}, ensure_ascii=False))
    else:
        _emit(text, style)


def cmd_before_compact(style: str = "claude"):
    if style not in {"claude","codex"}:
        _emit("[knowbase] unsupported_event：此宿主未验证支持压缩前 Hook，未执行。", style)
        return
    from .adapters import host_events
    result = host_events.handle(_read_stdin(), event="before-compact", style=style, defer_capture=True)
    if result.get("error"):
        print("[knowbase] 压缩前材料处理未完成：" + str(result["error"]), file=sys.stderr)


def _emit(text: str, style: str):
    """claude 风格=纯文本注入；zcode 风格=严格 JSON {"additionalContext": ...}。"""
    if not text:
        return
    _utf8_stdio()
    if style == "zcode":
        print(json.dumps({"additionalContext": text}, ensure_ascii=False))
    else:
        print(text)
