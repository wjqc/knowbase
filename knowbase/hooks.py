"""Hook 集成：轻量提示 / 阶段检索 / 静默增量入队。

职责划分（设计 6.1）：Hook = 触发器，LLM = 相关性裁判，MCP = 持久化。
核心函数供测试直接调用；cmd_* 包装层从 stdin 读 Claude Code hook JSON、
只注入有预算的相关提示，Stop 不输出阻断。要求轻量导入（不引 mcp/server）。
"""

import json
import os
import sys
import tempfile
from pathlib import Path

from . import config, index, store

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
        "只对相关候选memory_read；实际消费用memory_use绑定卡片版本与检索event_id，命中和阅读不等于有效帮助。\n"
        "2. 验证实际结果后再memory_feedback，outcome为helpful/not_helpful/outdated/incorrect，"
        "携带expected_content_revision、use_id、稳定task_outcome_id和真实task_result；没有采纳则无需反馈。\n"
        "3. 高价值技术经验与业务规则可保存；业务规则须有出处，候选按实际验证反馈演进。"
        "一次性评估/状态数字和repo/docs已有事实留在项目文档。\n"
        "4. Hook自动采集入队，不阻断原任务；普通待办不要求本会话领取、分页或逐段处理，"
        "无需为了入库或反馈继续回复。只有明确整理知识时再使用memory_job_*提炼协议。"
    )


# ---------- 2. 用户提示词：统一检索并注入命中 ----------


def _infer_scope(conn, project_dir=None) -> str | None:
    """从 hook 环境变量的项目目录推断当前项目 scope（目录名与 scope 精确匹配，未知项目不退化为全库）。"""
    project_dir = project_dir or os.environ.get("CLAUDE_PROJECT_DIR") or os.environ.get("ZCODE_PROJECT_DIR")
    if not project_dir:
        return None
    resolved = str(Path(project_dir).expanduser().resolve())
    mapped = config.mapped_scope(project_dir)
    if mapped:
        return str(mapped)
    name = Path(resolved).name.lower()
    if not name:
        return None
    scopes = [r[0] for r in conn.execute(
        "SELECT DISTINCT scope FROM meta WHERE staging = 0 AND scope != 'global'")]
    matches = [s for s in scopes if s and s.lower() == name]
    return max(matches, key=len) if matches else None


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


def user_prompt(prompt: str, project_dir=None, delivery_info=None) -> str:
    """对用户输入做本地检索（自动推断当前项目 scope），命中则返回注入文本。

    Phase 1 查询卫生（02 号设计§5）：先过意图门控与保守蒸馏再检索；
    长文/粘贴文档跳过检索与注入；技术标识原文保留。auto_search 永为本地检索。
    """
    hooks_cfg = config.load_config().get("hooks", {}) or {}
    if not hooks_cfg.get("enabled", True):
        return ""
    prompt = (prompt or "").strip()
    if len(prompt) < MIN_PROMPT_LEN:
        return ""
    from . import query_hygiene
    hy = query_hygiene.prepare(prompt)  # V1 safety limits cannot be disabled by config.
    rp = config.repo_path()
    if not rp.exists():
        return ""
    conn = index.connect(rp)
    try:
        if hy["action"] == "skip":
            # 跳过也留痕（query 存前 200 字符防日志膨胀），便于统计跳过原因分布。
            index.record_search(conn, prompt[:200], "", [], tool="auto_search",
                                query_class=f"skipped:{hy['reason']}", raw_len=len(prompt))
            return ""
        inferred = _infer_scope(conn, project_dir)
        if (project_dir or has_project_context()) and inferred is None:
            return "[knowbase] 当前项目未配置 scope_map，已跳过自动检索，避免跨项目召回。"
        scope = inferred or "global"
        diagnostics = {}
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
                            query_class=hy["query_class"], raw_len=len(prompt), retrieval=diagnostics)
        if delivery_info is not None:
            delivery_info["search_id"]=search_id
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


def _flag_path(session_id: str) -> Path:
    base = os.environ.get("KNOWBASE_HOOK_STATE") or tempfile.gettempdir()
    return Path(base) / f"knowbase-hook-{session_id}.flag"


def _mid_capture_flag_path(session_id: str) -> Path:
    """中途 capture 的 flag 文件路径（每个会话最多触发一次）。"""
    base = os.environ.get("KNOWBASE_HOOK_STATE") or tempfile.gettempdir()
    return Path(base) / f"knowbase-mid-capture-{session_id}.flag"


def _maybe_trigger_mid_session_capture(session_id: str, transcript_path: str) -> None:
    """M3-2: 检测 work_ops 达到阈值时触发中途 capture（不阻断）。

    每个会话最多触发一次中途 capture，通过 flag 文件控制。
    与 Stop Hook 的 capture 共享幂等键前缀，避免重复。
    """
    cfg = config.load_config()
    hooks_cfg = cfg.get("hooks", {}) or {}
    if not hooks_cfg.get("enabled", True):
        return
    # 检查是否启用中途 capture
    if not hooks_cfg.get("mid_session_capture", False):
        return
    # 检查是否已经触发过
    flag = _mid_capture_flag_path(session_id)
    if flag.exists():
        return
    # 获取阈值（默认 5 次 work_ops）
    threshold = hooks_cfg.get("mid_session_threshold", 5)
    # 统计 work_ops
    stats = _transcript_stats(transcript_path)
    if stats["work_ops"] < threshold:
        return
    # 达到阈值，尝试入队 capture
    job_id = _try_enqueue_capture(session_id, transcript_path, cfg)
    if job_id:
        # 设置 flag 防止重复触发
        try:
            flag.write_text("1", encoding="utf-8")
        except OSError:
            pass


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

def _read_stdin() -> dict:
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
        result = host_events.handle(data, event="recover", style=style)
        text += "\n" + host_events.render_foreground(result, data, event="recover", style=style)
    _emit(text, style)


def cmd_user_prompt(style: str = "claude"):
    os.environ.setdefault("KNOWBASE_AGENT_NAME", style)
    data = _read_stdin()
    prompt = _first(data, "prompt", "prompt_text", "user_prompt", "input")
    # M3-2: 中途检查点触发（不阻断，异步入队）
    sid = _first(data, "session_id", "sessionId")
    transcript = _first(data, "transcript_path", "transcriptPath", "transcript")
    if sid and transcript and not config.load_config()["inflight"]["enabled"]:
        _maybe_trigger_mid_session_capture(sid, transcript)
    delivery_info={}
    text = user_prompt(prompt, data.get("cwd") or data.get("project_dir"), delivery_info)
    if config.load_config()["inflight"]["enabled"]:
        from .adapters import host_events
        observation = host_events.handle(data, event="user-prompt", style=style)
        text = host_events.render_foreground(observation, data, event="user-prompt", style=style, prefix=text)
    from .retrieval_metrics import delivery
    delivery(config.repo_path(),delivery_info.get("search_id"),text,
             tiers=delivery_info.get("tiers"), dropped_once=delivery_info.get("dropped_once",0))
    _emit(text, style)


def cmd_stop(style: str = "claude"):
    """Always exit quietly: durable capture is independent of foreground continuation."""
    os.environ.setdefault("KNOWBASE_AGENT_NAME", style)
    data = _read_stdin()
    cfg = config.load_config()
    if not cfg.get("hooks", {}).get("enabled", True):
        return
    try:
        if cfg["inflight"]["enabled"] and cfg["inflight"]["capture_enabled"]:
            from .adapters import host_events
            result = host_events.handle(data, event="stop", style=style)
            if result.get("error"):
                print("[knowbase] Stop capture incomplete: " + str(result["error"]), file=sys.stderr)
        elif not cfg["inflight"]["enabled"]:
            sid = _first(data, "session_id", "sessionId") or "unknown"
            tp = _first(data, "transcript_path", "transcript", "transcriptPath")
            stop_event(sid, tp, allow_enqueue=cfg["inflight"]["capture_enabled"])
    except Exception as exc:
        print("[knowbase] Stop capture incomplete: " + type(exc).__name__, file=sys.stderr)


def cmd_tool_event(style: str = "claude", event: str = "tool-after"):
    os.environ.setdefault("KNOWBASE_AGENT_NAME", style)
    from .adapters import host_events
    data = _read_stdin()
    try:
        result = host_events.handle(data, event=event, style=style)
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
    result = host_events.handle(_read_stdin(), event="before-compact", style=style)
    if result.get("error"):
        print("[knowbase] 压缩前材料处理未完成：" + str(result["error"]), file=sys.stderr)


def _emit(text: str, style: str):
    """claude 风格=纯文本注入；zcode 风格=严格 JSON {"additionalContext": ...}。"""
    if not text:
        return
    if style == "zcode":
        print(json.dumps({"additionalContext": text}, ensure_ascii=False))
    else:
        print(text)
