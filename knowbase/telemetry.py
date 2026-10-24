"""最小命令遥测：不记录参数、查询、输出正文或身份。不可用时不影响业务。"""

import sqlite3
import uuid
from datetime import datetime,timezone
from functools import wraps
from time import monotonic

from . import config,index

MCP_COMMANDS=frozenset({"memory_save","memory_update","memory_read","memory_search","memory_feedback",
    "memory_context","memory_use","memory_stats","memory_capture","memory_job_claim","memory_job_material",
    "memory_job_renew","memory_job_submit","memory_job_status","memory_import"})
CLI_COMMANDS=frozenset({"init","import","reindex","verify","archive","promote","revise","list","stats",
    "history","dashboard","doctor","alerts","jobs","sync-once",
    "value.import","value.receipt","value.snapshot","value.usage",
    "value.register-member","value.confirm-member"})
# metrics / value.export / value.report 有意不在白名单：导出与报告路径对
# memory.db 保持只读，不因自身调用改变其报告的遥测口径（与 metrics 既有契约一致）。
HOOK_EVENTS=frozenset({"session-start","user-prompt","stop","tool-before","tool-after","tool-failure","before-compact"})


_INSTALLATION_CACHE: dict[str, str] = {}


def host_name(value):
    value = str(value or "unknown").lower()
    if value in {"claude", "claude-code", "claudecode"}:
        return "claude-code"
    return value if value in {"zcode", "trae", "cli"} else "unknown"


def _installation_id(path) -> str:
    """best-effort 读取本机 installation_id；无表/未登记返回空串，绝不冒充成员。"""
    key = str(path)
    if key in _INSTALLATION_CACHE:
        return _INSTALLATION_CACHE[key]
    value = ""
    try:
        conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.05)
        try:
            row = conn.execute("SELECT value FROM value_identity WHERE key='installation_id'").fetchone()
            if row and row[0]:
                value = row[0]
        finally:
            conn.close()
    except (sqlite3.Error, OSError, ValueError, AttributeError):
        pass
    if value:
        _INSTALLATION_CACHE[key] = value
    return value


def record(transport,command,status,duration_ms):
    allowed=MCP_COMMANDS if transport=="mcp" else CLI_COMMANDS|{f"hook.{e}" for e in HOOK_EVENTS}
    if command not in allowed or status not in {"ok","returned_error","raised"}:
        return
    conn=None
    try:
        path=index.db_path(config.repo_path()).resolve()
        if not path.exists():
            return
        conn=sqlite3.connect(path.as_uri()+"?mode=rw",uri=True,timeout=0.05)
        call=(uuid.uuid4().hex,datetime.now(timezone.utc).isoformat(timespec="microseconds"),
              transport,command,status,max(0,duration_ms))
        try:
            conn.execute("INSERT INTO command_calls VALUES(?,?,?,?,?,?,?)", (*call,_installation_id(path)))
        except sqlite3.OperationalError:
            conn.execute("INSERT INTO command_calls VALUES(?,?,?,?,?,?)",call)
        try:
            # Keep command_calls' positional layout compatible with already-running
            # old MCP processes. Their calls continue to count with unknown host.
            host = host_name(config.agent_name()) if transport != "cli" or command.startswith("hook.") else "cli"
            conn.execute("INSERT INTO command_call_hosts VALUES(?,?)", (call[0],host))
        except sqlite3.OperationalError:
            pass
        conn.commit()
    except (sqlite3.Error,OSError,ValueError,TypeError):
        pass
    finally:
        if conn:
            conn.close()


def observe(function,command):
    @wraps(function)
    def observed(*args,**kwargs):
        import json
        started=monotonic();status="raised"
        try:
            result=function(*args,**kwargs)
            status="ok"
            if isinstance(result,str):
                if result.startswith("错误"):
                    status="returned_error"
                elif result.startswith("{"):
                    try:
                        if json.loads(result).get("error"):
                            status="returned_error"
                    except (ValueError,AttributeError):
                        pass
            return result
        finally:
            record("mcp",command,status,(monotonic()-started)*1000)
    return observed
