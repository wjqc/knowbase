"""接入就绪状态与一次性提示（P4）。

四个状态分别输出，不互相掩盖：
- config_complete：配置文件存在、知识仓库存在且为 Git 仓库；
- protocol_pass：迁移全部应用、索引三边计数一致、无解析失败卡片；
- hosts_loaded：有会话以当前 build 启动（按 agent_name 计）；
- real_task_verified：存在已完成的真实任务结果。

非阻断性问题不持续向普通 Prompt 注入：同一提示键只提示一次
（首次状态变化、服务不可用或需授权时）；已提示过的键不再重复出现。
"""

import sqlite3
from contextlib import closing
from pathlib import Path

from . import build_identity, config, gitops, index, sessions, store


def _protocol_pass(repo: Path) -> tuple[bool, str]:
    """迁移 + 索引一致性（只读）。"""
    from . import migrations
    path = index.db_path(repo)
    if not path.exists():
        return False, "memory.db 不存在"
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
            conn.execute("PRAGMA query_only=ON")
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
            if "schema_migrations" not in tables:
                return False, "schema_migrations 缺失"
            applied = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
            pending = [v for v, _ in migrations.MIGRATIONS if v not in applied]
            if pending:
                return False, f"待应用迁移：{pending}"
            if "meta" not in tables or "mem_fts" not in tables:
                return False, "meta/mem_fts 缺失"
            meta_n = conn.execute("SELECT count(*) FROM meta").fetchone()[0]
            fts_n = conn.execute("SELECT count(*) FROM mem_fts").fetchone()[0]
    except sqlite3.Error as exc:
        return False, f"数据库读取失败：{exc}"
    errors: list = []
    cards = list(store.iter_all(repo, include_staging=True, errors=errors))
    if errors:
        return False, f"解析失败卡片 {len(errors)} 张"
    if meta_n != fts_n or meta_n != len(cards):
        return False, f"索引计数不一致：markdown={len(cards)} meta={meta_n} fts={fts_n}"
    return True, ""


def readiness(repo) -> dict:
    """四项就绪状态 + 需重连会话（只读，不写库、不触网）。"""
    repo = Path(repo)
    cfg = config.load_config()
    cfg_ok = (config.CONFIG_PATH.exists() and repo.exists()
              and gitops.is_repo(repo))
    proto_ok, proto_reason = _protocol_pass(repo)
    current_build = build_identity.build_id()
    grouped = sessions.sessions_by_agent(current_build)
    hosts_loaded = sorted(grouped["current"])
    reconnect = sorted(grouped["stale"])
    real_verified = False
    try:
        path = index.db_path(repo)
        if path.exists():
            with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
                conn.execute("PRAGMA query_only=ON")
                tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
                if "task_outcomes" in tables:
                    row = conn.execute(
                        "SELECT COUNT(*) FROM task_outcomes WHERE result='completed'"
                    ).fetchone()
                    real_verified = bool(row and row[0] > 0)
    except sqlite3.Error:
        real_verified = False
    return {
        "config_complete": cfg_ok,
        "protocol_pass": proto_ok,
        "protocol_reason": proto_reason,
        "hosts_loaded": hosts_loaded,
        "real_task_verified": real_verified,
        "reconnect_required": reconnect,
        "current_build": current_build,
        "repo": str(repo),
    }


def pending_notices(repo, ready: dict | None = None) -> list[dict]:
    """当前应提示且尚未提示过的事项（只计算，不标记）。"""
    ready = ready if ready is not None else readiness(repo)
    candidates: list[dict] = []
    if not ready["config_complete"]:
        candidates.append({"key": "config_incomplete",
                           "message": "配置或知识仓库未就绪：请先运行 knowbase init"})
    if not ready["protocol_pass"]:
        candidates.append({"key": "protocol_fail",
                           "message": f"协议未通过：{ready['protocol_reason']}"})
    for agent in ready["reconnect_required"]:
        candidates.append({"key": f"reconnect_required:{agent}",
                           "message": f"宿主 {agent} 的会话仍为旧 build，重连前请重启该宿主的 knowbase serve"})
    if not ready["real_task_verified"]:
        candidates.append({"key": "no_real_task_verified",
                           "message": "尚未有已完成的真实任务验证；就绪状态不以此为阻断"})
    notified = sessions.notified_keys()
    return [c for c in candidates if c["key"] not in notified]


def report(repo) -> dict:
    """就绪状态 + 本次新提示（调用方打印后应 mark_notified）。"""
    ready = readiness(repo)
    return {"readiness": ready, "new_notices": pending_notices(repo, ready)}
