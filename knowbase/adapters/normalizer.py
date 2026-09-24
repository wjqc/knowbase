"""transcript 事件归一化。

不同宿主的 transcript 格式不同；统一归一化为 NormalizedEvent。
"""

import csv
import io
import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class NormalizedEvent:
    """归一化后的对话事件。"""
    session_id: str
    message_id: str
    role: str              # "user" | "assistant" | "tool"
    text: str
    tool_call_id: str = ""
    tool_name: str = ""
    tool_result: str = ""
    timestamp: str = ""
    project_scope: str = ""
    meta: dict = field(default_factory=dict)


def normalize_event(raw: dict, *, default_session_id: str = "",
                    default_scope: str = "") -> NormalizedEvent:
    """将原始 transcript 行归一化。

    支持格式：
    - Claude Code: {"type": "tool_use", "name": "...", "input": {...}}
    - ZCode: {"role": "user", "content": "..."}
    - 通用: {"session_id": "...", "role": "...", "text": "..."}
    """
    session_id = (raw.get("session_id") or raw.get("sessionId")
                  or default_session_id or "unknown")
    message_id = raw.get("message_id") or raw.get("id") or ""
    role = raw.get("role", "")
    text = ""
    tool_call_id = ""
    tool_name = ""
    tool_result = ""
    timestamp = raw.get("timestamp") or raw.get("ts") or ""
    project_scope = raw.get("project_scope") or raw.get("scope") or default_scope

    event_type = raw.get("type", "")

    if event_type == "tool_use":
        role = "tool"
        tool_name = raw.get("name", "")
        tool_call_id = raw.get("tool_call_id") or raw.get("id", "")
        tool_input = raw.get("input") or {}
        text = json.dumps(tool_input, ensure_ascii=False)[:2000]
    elif event_type == "tool_result":
        role = "tool"
        tool_call_id = raw.get("tool_call_id", "")
        tool_result = raw.get("content") or raw.get("text") or ""
        if isinstance(tool_result, list):
            tool_result = "\n".join(
                item.get("text", "") if isinstance(item, dict) else str(item)
                for item in tool_result
            )
        text = tool_result[:4000]
    elif role in ("user", "assistant", "system"):
        content = raw.get("content") or raw.get("text") or ""
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict):
                    if item.get("type") == "text":
                        parts.append(item.get("text", ""))
                    elif item.get("type") == "tool_use":
                        tool_name = item.get("name", "")
                        tool_call_id = item.get("id", "")
                        parts.append(f"[tool:{tool_name}]")
                    elif item.get("type") == "tool_result":
                        parts.append(str(item.get("content", "")))
                else:
                    parts.append(str(item))
            text = "\n".join(parts)
        else:
            text = str(content)
    else:
        text = json.dumps(raw, ensure_ascii=False)[:2000]

    return NormalizedEvent(
        session_id=session_id,
        message_id=message_id,
        role=role,
        text=text[:8000],  # 截断过长内容
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        tool_result=tool_result[:4000] if tool_result else "",
        timestamp=timestamp,
        project_scope=project_scope,
    )


def parse_transcript_file(path: str, *, default_session_id: str = "",
                          default_scope: str = "") -> list[NormalizedEvent]:
    """解析 transcript 文件（JSONL 格式）。"""
    events = []
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line)
                    if isinstance(raw, dict):
                        events.append(normalize_event(
                            raw, default_session_id=default_session_id,
                            default_scope=default_scope,
                        ))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return events


def parse_transcript_auto(path: str, *, default_session_id: str = "",
                          default_scope: str = "") -> list[NormalizedEvent]:
    """M3-5: 自动检测格式并解析 transcript 文件。

    支持格式：
    - JSONL: 每行一个 JSON object（现有格式）
    - JSON array: 整个文件是一个 JSON array
    - CSV: 含 role/text/timestamp 列的 CSV 文件

    格式检测：
    - 首字符 '[' → JSON array
    - 首行含逗号且为表头 → CSV
    - 否则 → JSONL
    """
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            content = f.read()
    except OSError:
        return []

    if not content.strip():
        return []

    # 检测格式
    stripped = content.strip()

    # JSON array: 首字符 '['
    if stripped.startswith("["):
        return _parse_json_array(stripped, default_session_id, default_scope)

    # CSV: 首行含逗号且看起来像表头
    first_line = content.split("\n", 1)[0].strip()
    if "," in first_line and _looks_like_csv_header(first_line):
        return _parse_csv(content, default_session_id, default_scope)

    # 默认: JSONL
    return _parse_jsonl(content, default_session_id, default_scope)


def _looks_like_csv_header(line: str) -> bool:
    """检测一行是否看起来像 CSV 表头。"""
    # 常见的 CSV 表头列名
    header_keywords = {"role", "text", "content", "timestamp", "ts", "message",
                       "session", "user", "assistant"}
    parts = [p.strip().lower() for p in line.split(",")]
    return any(kw in parts for kw in header_keywords)


def _parse_jsonl(content: str, default_session_id: str, default_scope: str) -> list[NormalizedEvent]:
    """解析 JSONL 格式。"""
    events = []
    for line in content.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
            if isinstance(raw, dict):
                events.append(normalize_event(
                    raw, default_session_id=default_session_id,
                    default_scope=default_scope,
                ))
        except json.JSONDecodeError:
            continue
    return events


def _parse_json_array(content: str, default_session_id: str, default_scope: str) -> list[NormalizedEvent]:
    """解析 JSON array 格式。"""
    try:
        data = json.loads(content)
        if not isinstance(data, list):
            return []
        events = []
        for item in data:
            if isinstance(item, dict):
                events.append(normalize_event(
                    item, default_session_id=default_session_id,
                    default_scope=default_scope,
                ))
        return events
    except json.JSONDecodeError:
        return []


def _parse_csv(content: str, default_session_id: str, default_scope: str) -> list[NormalizedEvent]:
    """解析 CSV 格式。"""
    events = []
    try:
        reader = csv.DictReader(io.StringIO(content))
        for row in reader:
            # 映射常见的 CSV 列名
            raw = {
                "role": row.get("role") or row.get("Role") or row.get("ROLE") or "",
                "text": row.get("text") or row.get("Text") or row.get("TEXT") or
                        row.get("content") or row.get("Content") or row.get("message") or "",
                "timestamp": row.get("timestamp") or row.get("Timestamp") or
                             row.get("ts") or row.get("Ts") or "",
                "session_id": row.get("session_id") or row.get("sessionId") or
                              row.get("session") or default_session_id,
                "project_scope": row.get("scope") or row.get("project_scope") or default_scope,
            }
            if raw["role"] or raw["text"]:
                events.append(normalize_event(
                    raw, default_session_id=default_session_id,
                    default_scope=default_scope,
                ))
    except Exception:
        pass
    return events
