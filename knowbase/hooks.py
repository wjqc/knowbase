"""Hook 集成：会话开始提醒 / 提示词自动检索 / 会话结束漏存再判断。

职责划分（设计 6.1）：Hook = 触发器，LLM = 相关性裁判，MCP = 持久化。
核心函数供测试直接调用；cmd_* 包装层从 stdin 读 Claude Code hook JSON、
向 stdout 产出注入文本或阻断 JSON。要求轻量导入（不引 mcp/server）。
"""

import json
import os
import sys
import tempfile
from pathlib import Path

from . import config, index, store

MIN_PROMPT_LEN = 6
MAX_HITS = 3
MEMORY_WRITE_TOOLS = ("memory_save", "memory_update", "memory_feedback")
MEMORY_READ_TOOLS = ("memory_read",)
FEEDBACK_TOOL = "memory_feedback"
WORK_TOOLS = ("Edit", "Write", "NotebookEdit", "Bash")


# ---------- 1. 会话开始：注入任务规则 ----------

def session_start() -> str:
    return (
        "[knowbase 经验库] 本会话任务规则：\n"
        "1. 任务涉及具体项目/系统/报错/环境配置 → 先 memory_search（用 ≥3 字技术词，如\"EasyConnect 死锁\"）；\n"
        "2. 任务结束先过可复用过滤：已修复的一次性 bug、repo/git/docs 能回答的事实、"
        "一次性的评测/测试结果数字 → 不入库（放 progress/docs）；"
        "剩下的\"下次还会踩/还会用\"→ memory_save（提示相似时改用 memory_update）；\n"
        "3. 按记忆行动或验证后 → 必须回填 memory_feedback（helpful/not_helpful/outdated/incorrect）；"
        "once→verified 晋升与 active→stale 淘汰只认反馈，不回填状态机不转；\n"
        "4. stale/once 状态的记忆采信前先在当前环境验证。"
    )


# ---------- 2. 用户提示词：统一检索并注入命中 ----------


def _infer_scope(conn) -> str | None:
    """从 hook 环境变量的项目目录推断当前项目 scope（目录名与 scope 精确匹配，未知项目不退化为全库）。"""
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR") or os.environ.get("ZCODE_PROJECT_DIR")
    if not project_dir:
        return None
    resolved = str(Path(project_dir).expanduser().resolve())
    scope_map = config.load_config().get("scope_map", {}) or {}
    mapped = scope_map.get(resolved) or scope_map.get(Path(resolved).name)
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


def user_prompt(prompt: str) -> str:
    """对用户输入做本地检索（自动推断当前项目 scope），命中则返回注入文本。"""
    if not config.load_config().get("hooks", {}).get("enabled", True):
        return ""
    prompt = (prompt or "").strip()
    if len(prompt) < MIN_PROMPT_LEN:
        return ""
    rp = config.repo_path()
    if not rp.exists():
        return ""
    conn = index.connect(rp)
    inferred = _infer_scope(conn)
    if has_project_context() and inferred is None:
        conn.close()
        return "[knowbase] 当前项目未配置 scope_map，已跳过自动检索，避免跨项目召回。"
    scope = inferred or "global"
    hits = index.search(conn, prompt, scope=scope, limit=100)
    min_conf = (config.load_config().get("hooks", {}) or {}).get("inject_min_confidence", "verified")
    if min_conf == "verified":
        hits = [h for h in hits if h.get("confidence") == "verified"]  # 防污染开关：自动注入只取已验证经验
    # 矛盾知识只供主动调查，不自动建议采用。
    hits = [h for h in hits if not any(r.get("type") == "contradicts"
            for r in (store.load(rp, h["id"])[0] or {}).get("relations", []))]
    hits = hits[:MAX_HITS]
    index.record_search(conn, prompt, scope, hits, tool="auto_search")
    conn.close()
    if not hits:
        return ""
    lines = ["[knowbase 自动检索] 以下为统一混合检索候选，尚未确认适用；先核对项目、版本与证据，再按需 memory_read："]
    for h in hits:
        lines.append(f"- [{h['id']}] {h['title']}（{h['confidence']}·{h['status']} · scope={h['scope']}）")
    return "\n".join(lines)


# ---------- 3. 会话结束：漏存再判断 / 读后未回填提醒（每次会话最多阻断一次） ----------

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


def stop_event(session_id: str, transcript_path: str) -> str:
    """漏存（有操作无沉淀）或读后未回填 → 返回阻断 JSON（每会话限一次）；否则返回空串。"""
    cfg = config.load_config()
    if not cfg.get("hooks", {}).get("enabled", True):
        return ""
    t = _transcript_stats(transcript_path)
    if t["work_ops"] and not t["memory_write"]:
        reason = (
            "[knowbase] 本次会话有代码/文件操作但未沉淀经验。先过可复用过滤"
            "（已修复的一次性 bug、repo/git/docs 能回答的事实、一次性结果数字不入库），"
            "若仍有值得复用的踩坑/决策/固化流程/用户约束，先 memory_search 查重，"
            "再 memory_save 保存（提示相似则 memory_update）；"
            "确认没有可复用经验，则直接回复“无可沉淀经验”结束。"
            "本提醒每次会话最多出现一次。"
        )
    elif t["memory_read"] and not t["feedback"]:
        reason = (
            "[knowbase] 本次会话读取过记忆但未回填使用反馈。"
            "请对实际参考/采纳的条目调用 "
            "memory_feedback(id, helpful/not_helpful/outdated/incorrect)——"
            "once→verified 晋升与 active→stale 淘汰只认反馈，缺反馈的记忆会永远停在低置信状态。"
            "若读过的条目均未采纳，直接回复“未采纳，无需回填”结束。"
            "本提醒每次会话最多出现一次。"
        )
    else:
        return ""
    flag = _flag_path(session_id)
    if flag.exists():
        return ""  # 已提醒过一次，不再打断
    try:
        flag.write_text("1", encoding="utf-8")
    except OSError:
        pass
    return json.dumps({"decision": "block", "reason": reason}, ensure_ascii=False)


# ---------- stdin/stdout 包装层 ----------

def _read_stdin() -> dict:
    try:
        return json.load(sys.stdin)
    except Exception:
        return {}


def _first(d: dict, *keys) -> str:
    for k in keys:
        if d.get(k):
            return str(d[k])
    return ""


def cmd_session_start(style: str = "claude"):
    _emit(session_start(), style)


def cmd_user_prompt(style: str = "claude"):
    data = _read_stdin()
    prompt = _first(data, "prompt", "prompt_text", "user_prompt", "input")
    _emit(user_prompt(prompt), style)


def cmd_stop(style: str = "claude"):
    data = _read_stdin()
    sid = _first(data, "session_id", "sessionId") \
        or os.environ.get("CLAUDE_SESSION_ID") or os.environ.get("ZCODE_SESSION_ID") or "unknown"
    tp = _first(data, "transcript_path", "transcript", "transcriptPath")
    out = stop_event(sid, tp)
    if out:
        print(out)  # 顶层 decision/reason：claude 与 zcode 同形


def _emit(text: str, style: str):
    """claude 风格=纯文本注入；zcode 风格=严格 JSON {"additionalContext": ...}。"""
    if not text:
        return
    if style == "zcode":
        print(json.dumps({"additionalContext": text}, ensure_ascii=False))
    else:
        print(text)
