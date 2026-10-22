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
    "history","dashboard","doctor","alerts","jobs","sync-once"})
HOOK_EVENTS=frozenset({"session-start","user-prompt","stop","tool-before","tool-after","tool-failure","before-compact"})


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
        conn.execute("INSERT INTO command_calls VALUES(?,?,?,?,?,?)",
            (uuid.uuid4().hex,datetime.now(timezone.utc).isoformat(timespec="microseconds"),
             transport,command,status,max(0,duration_ms)))
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
