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

from . import config, index

MIN_PROMPT_LEN = 6
MAX_HITS = 3
MEMORY_TOOLS = ("memory_save", "memory_update", "memory_feedback")
WORK_TOOLS = ("Edit", "Write", "NotebookEdit", "Bash")


# ---------- 1. 会话开始：注入任务规则 ----------

def session_start() -> str:
    return (
        "[knowbase 经验库] 本会话任务规则：\n"
        "1. 任务涉及具体项目/系统/报错/环境配置 → 先 memory_search（用 ≥3 字技术词，如\"EasyConnect 死锁\"）；\n"
        "2. 任务结束：产生了踩坑/决策/固化流程/用户约束 → memory_save（提示相似时改用 memory_update）；\n"
        "3. 按某条记忆行动后 → memory_feedback 回填 helpful/not_helpful/outdated/incorrect；\n"
        "4. stale/once 状态的记忆采信前先在当前环境验证。"
    )


# ---------- 2. 用户提示词：本地自动检索并注入命中 ----------

def _keywords(prompt: str) -> list[str]:
    """从自然语言提示词提取检索关键词：ASCII 词 + 中文短段（长段切 4 字滑窗）。"""
    import re
    kws = [w for w in re.findall(r"[A-Za-z0-9][A-Za-z0-9_.\-]+", prompt) if len(w) >= 2]
    for run in re.findall(r"[\u4e00-\u9fff]{2,}", prompt):
        if len(run) <= 4:
            kws.append(run)
        else:
            kws.extend(run[i:i + 4] for i in range(0, len(run) - 3, 3))
    seen, out = set(), []
    for k in kws:
        if k.lower() not in seen:
            seen.add(k.lower())
            out.append(k)
    return out[:24]


def _scored_search(conn, kws: list[str], limit: int) -> list[dict]:
    """OR 语义兜底：按命中关键词个数打分（AND 语义对无空格长句必然漏检）。"""
    scored = []
    for r in conn.execute("SELECT * FROM meta WHERE status != 'archived' AND staging = 0"):
        m = index._row_meta(r)
        row = conn.execute("SELECT title, tags, body FROM mem_fts WHERE id=?", (m["id"],)).fetchone()
        hay = " ".join(row).lower() if row else ""
        score = sum(1 for k in kws if k.lower() in hay)
        if score:
            scored.append((score, m))
    scored.sort(key=lambda x: -x[0])
    return [{**m, "score": s} for s, m in scored[:limit]]


def user_prompt(prompt: str) -> str:
    """对用户输入做本地检索，命中则返回注入文本；无命中返回空串。"""
    prompt = (prompt or "").strip()
    if len(prompt) < MIN_PROMPT_LEN:
        return ""
    rp = config.repo_path()
    if not rp.exists():
        return ""
    kws = _keywords(prompt)
    if not kws:
        return ""
    conn = index.connect(rp)
    ascii_kws = [k for k in kws if k[0].isascii()]
    hits = index.search(conn, " ".join(ascii_kws), limit=MAX_HITS) if ascii_kws else []
    if not hits:
        hits = _scored_search(conn, kws, MAX_HITS)
    if hits:
        index.record_usage(conn, "auto_search", detail=prompt[:80])
    conn.close()
    if not hits:
        return ""
    lines = ["[knowbase 自动检索] 以下历史经验与当前任务相关，动手前先 memory_read 对应条目："]
    for h in hits:
        lines.append(f"- [{h['id']}] {h['title']}（{h['confidence']}·{h['status']}）")
    return "\n".join(lines)


# ---------- 3. 会话结束：漏存再判断（每次会话最多阻断一次） ----------

def _transcript_stats(path: str) -> tuple[int, int]:
    """扫描会话 transcript：返回 (记忆工具调用次数, 实质文件/命令操作次数)。"""
    memory_ops = work_ops = 0
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            for line in f:
                if not memory_ops and any(f'"{t}"' in line for t in MEMORY_TOOLS):
                    memory_ops += 1
                if '"tool_use"' in line and any(f'"{t}"' in line for t in WORK_TOOLS):
                    work_ops += 1
    except OSError:
        pass
    return memory_ops, work_ops


def _flag_path(session_id: str) -> Path:
    base = os.environ.get("KNOWBASE_HOOK_STATE") or tempfile.gettempdir()
    return Path(base) / f"knowbase-hook-{session_id}.flag"


def stop_event(session_id: str, transcript_path: str) -> str:
    """有实质操作但未沉淀 → 返回阻断 JSON（每会话限一次）；否则返回空串。"""
    cfg = config.load_config()
    if not cfg.get("hooks", {}).get("enabled", True):
        return ""
    memory_ops, work_ops = _transcript_stats(transcript_path)
    if memory_ops or not work_ops:
        return ""
    flag = _flag_path(session_id)
    if flag.exists():
        return ""  # 已提醒过一次，不再打断
    try:
        flag.write_text("1", encoding="utf-8")
    except OSError:
        pass
    return json.dumps({
        "decision": "block",
        "reason": (
            "[knowbase] 本次会话有代码/文件操作但未沉淀经验。请判断："
            "若有值得复用的踩坑/决策/固化流程/用户约束，先 memory_search 查重，"
            "再 memory_save 保存（提示相似则 memory_update）；"
            "确认没有可复用经验，则直接回复“无可沉淀经验”结束。"
            "本提醒每次会话最多出现一次。"
        ),
    }, ensure_ascii=False)


# ---------- stdin/stdout 包装层 ----------

def _read_stdin() -> dict:
    try:
        return json.load(sys.stdin)
    except Exception:
        return {}


def cmd_session_start():
    print(session_start())


def cmd_user_prompt():
    data = _read_stdin()
    out = user_prompt(str(data.get("prompt", "")))
    if out:
        print(out)


def cmd_stop():
    data = _read_stdin()
    out = stop_event(str(data.get("session_id", "unknown")),
                     str(data.get("transcript_path", "")))
    if out:
        print(out)
