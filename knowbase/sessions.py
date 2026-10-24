"""本机会话与通知状态（P4 观测层）。

会话记录只证明"某宿主确实以当前 build 启动过 serve"，不认证身份：
agent_name 来自各宿主 MCP 配置声明的 KNOWBASE_AGENT_NAME。
文件全部落在配置文件同目录（默认 ~/.knowbase），0600 保存，不进 Git。
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from . import config

MAX_SESSIONS = 50


def _sessions_path() -> Path:
    return config.CONFIG_PATH.parent / "sessions.json"


def _notices_path() -> Path:
    return config.CONFIG_PATH.parent / "notices.json"


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps(data, ensure_ascii=False, indent=2))
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def record_session(agent_name: str, repo: str, build_id: str, runtime: str) -> dict:
    """记录一次 serve 启动；失败时调用方负责吞掉（绝不能阻断 serve）。"""
    entry = {"agent_name": agent_name or "unknown", "repo": str(repo),
             "build_id": build_id, "runtime": runtime,
             "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    sessions = _read_json(_sessions_path(), [])
    if not isinstance(sessions, list):
        sessions = []
    sessions.append(entry)
    sessions = sessions[-MAX_SESSIONS:]
    _write_json(_sessions_path(), sessions)
    return entry


def list_sessions() -> list[dict]:
    """全部已记录会话；文件缺失或损坏时返回空。"""
    sessions = _read_json(_sessions_path(), [])
    return sessions if isinstance(sessions, list) else []


def sessions_by_agent(current_build_id: str) -> dict:
    """按宿主分组：以每宿主最新一条会话为准。

    最新会话为当前 build → current（已加载）；最新会话为旧 build →
    stale（需重连）。旧会话不因一条新会话已加载而继续告警。
    """
    latest: dict[str, dict] = {}
    for s in list_sessions():
        agent = s.get("agent_name") or "unknown"
        prev = latest.get(agent)
        if prev is None or s.get("started_at", "") > prev.get("started_at", ""):
            latest[agent] = s
    current = {a: s for a, s in latest.items() if s.get("build_id") == current_build_id}
    stale = {a: s for a, s in latest.items() if s.get("build_id") != current_build_id}
    return {"current": current, "stale": stale}


def notified_keys() -> set:
    keys = _read_json(_notices_path(), [])
    return set(keys) if isinstance(keys, list) else set()


def mark_notified(keys) -> None:
    merged = sorted(notified_keys() | {str(k) for k in keys})
    _write_json(_notices_path(), merged)
