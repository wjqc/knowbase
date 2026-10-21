"""隐私最小化的可移植本机汇总；只读SQLite，不迁移、不触网、不复制原文。"""

import hashlib
import json
import re
import sqlite3
from datetime import datetime,timezone
from zoneinfo import ZoneInfo,ZoneInfoNotFoundError

from . import index,telemetry

SCHEMA="knowbase.metrics.v1"


def _time(value,naive_tz):
    try:
        stamp=datetime.fromisoformat(value.replace("Z","+00:00"))
        return stamp.replace(tzinfo=naive_tz).timestamp() if stamp.tzinfo is None else stamp.timestamp()
    except (ValueError,TypeError,AttributeError,OverflowError):
        return None


def export(repo,*,since="",until="",source_label="unknown",naive_timezone="local"):
    if not re.fullmatch(r"[a-zA-Z0-9_.-]{1,64}",source_label):
        return {"error":"INVALID_SOURCE_LABEL"}
    if naive_timezone=="local":
        naive_tz=datetime.now().astimezone().tzinfo
        tz_label="fixed-offset-at-export:"+datetime.now(naive_tz).strftime("%z")
    else:
        try:
            naive_tz=ZoneInfo(naive_timezone)
        except (ZoneInfoNotFoundError,ValueError):
            return {"error":"INVALID_NAIVE_TIMEZONE"}
        tz_label=naive_timezone
    # 无时区的报告边界定义为UTC；历史无时区日志单独按声明解释。
    lower=_time(since,timezone.utc) if since else None
    upper=_time(until,timezone.utc) if until else datetime.now(timezone.utc).timestamp()
    if (since and lower is None) or upper is None or (lower is not None and lower>=upper):
        return {"error":"INVALID_METRICS_WINDOW"}
    path=index.db_path(repo).resolve()
    if not path.exists():
        return {"error":"MEMORY_DB_NOT_FOUND"}
    conn=None
    try:
        conn=sqlite3.connect(path.as_uri()+"?mode=ro",uri=True,timeout=0.2)
        conn.execute("PRAGMA query_only=ON")
        conn.create_function("metric_time",1,lambda value:_time(value,naive_tz),deterministic=True)
        conn.execute("BEGIN")
        tables={r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        lower=lower if lower is not None else -62135596800.0
        def in_window(column):
            return f"metric_time({column})>=? AND metric_time({column})<?"
        def grouped(table,column,values,stamp="ts"):
            if table not in tables:
                return None
            counts={}
            for key,count in conn.execute(f"SELECT {column},COUNT(*) FROM {table} WHERE "+in_window(stamp)+f" GROUP BY {column}",(lower,upper)):
                safe=key if key in values else "other"
                counts[safe]=counts.get(safe,0)+count
            return counts
        legacy=grouped("usage_log","tool",{"search","auto_search","inflight_search","read","save","update","feedback","import"})
        commands=None
        if "command_calls" in tables:
            commands=[]
            valid=telemetry.MCP_COMMANDS|telemetry.CLI_COMMANDS|{f"hook.{e}" for e in telemetry.HOOK_EVENTS}
            merged={}
            for transport,command,status,count in conn.execute("SELECT transport,command,status,COUNT(*) FROM command_calls WHERE "+in_window("ts")+
                " GROUP BY transport,command,status",(lower,upper)):
                key=(transport if transport in {"mcp","cli"} else "other",command if command in valid else "other",
                     status if status in {"ok","returned_error","raised"} else "other")
                merged[key]=merged.get(key,0)+count
            commands=[{"transport":key[0],"command":key[1],"status":key[2],"count":count} for key,count in sorted(merged.items())]
        counts={"command_calls":commands,"legacy_usage_records":legacy,
            "context_status":grouped("context_events","status",{"no_hit","executed","failed","skipped_cache","deferred_budget","deferred_sync","scope_unresolved"},"created_at"),
            "use_events":grouped("knowledge_uses","state",{"read","referenced","adopted","irrelevant","unverified"},"created_at"),
            "feedback_log_observations":grouped("feedback_log","outcome",{"helpful","not_helpful","outdated","incorrect"}),
            "feedback_bindings":grouped("feedback_events","binding",{"version_task","legacy_unbound"},"created_at"),
            "feedback_receipt_states":grouped("feedback_events","state",{"applying","applied"},"created_at")}
        counts["context_triggers"]=grouped("context_events","trigger",{"task_plan","subtask_start","new_failure",
            "decision","verified_result","checkpoint","before_compact","recover","observe"},"created_at")
        if "knowledge_uses" in tables:
            # 一条任务-卡版本只取窗口内最后的明确状态，避免参考→采纳被算成两个任务。
            rows=conn.execute("SELECT state,COUNT(*) FROM (SELECT state,ROW_NUMBER() OVER (PARTITION BY "
                "host_profile,session_id,task_id,task_revision,memory_id,content_revision "
                "ORDER BY created_at DESC,rowid DESC) AS rank FROM knowledge_uses WHERE state<>'read' AND "+
                in_window("created_at")+") WHERE rank=1 GROUP BY state",(lower,upper)).fetchall()
            latest={}
            for state,count in rows:
                state=state if state in {"referenced","adopted","irrelevant","unverified"} else "other"
                latest[state]=latest.get(state,0)+count
            counts["latest_task_card_version_states"]=latest
        else:
            counts["latest_task_card_version_states"]=None
        invalid={};coverage={}
        for table,stamp in (("usage_log","ts"),("command_calls","ts"),("context_events","created_at"),
                            ("knowledge_uses","created_at"),("feedback_log","ts"),("feedback_events","created_at")):
            if table in tables:
                invalid[table]=conn.execute(f"SELECT COUNT(*) FROM {table} WHERE metric_time({stamp}) IS NULL").fetchone()[0]
                first,last=conn.execute(f"SELECT MIN(metric_time({stamp})),MAX(metric_time({stamp})) FROM {table}").fetchone()
                coverage[table]={"first_recorded_at":datetime.fromtimestamp(first,timezone.utc).isoformat() if first is not None else None,
                    "last_recorded_at":datetime.fromtimestamp(last,timezone.utc).isoformat() if last is not None else None}
        snapshot=None
        if "meta" in tables:
            snapshot={"basis":"current_index_snapshot_not_window_activity","by_type":{}}
            for kind,count in conn.execute("SELECT type,COUNT(*) FROM meta GROUP BY type"):
                kind=kind if kind in {"pitfall","decision","workflow","preference","standard","bizrule","reference"} else "other"
                snapshot["by_type"][kind]=snapshot["by_type"].get(kind,0)+count
        version=conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] if "schema_migrations" in tables else None
        result={"schema":SCHEMA,"generated_at":datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source":{"label":source_label,"coverage":"local_device_only","identity":"operator_label_unverified"},
            "window":{"since":datetime.fromtimestamp(lower,timezone.utc).isoformat() if since else None,
                      "until":datetime.fromtimestamp(upper,timezone.utc).isoformat(),"bounds":"since_inclusive_until_exclusive",
                      "naive_log_timezone":tz_label},"database_schema":version,"counts":counts,
            "invalid_timestamp_records":invalid,"logging_coverage_all_time":coverage,"inventory_snapshot":snapshot,
            "limitations":["Counts are local records, not a team total or verified human count.",
                "Legacy usage is incomplete and may contain historical duplicates; never add it to command_calls.",
                "New command_calls count observed completed attempts, including retries; process death or telemetry failure may omit calls.",
                "Use states are host declarations, not independently verified outcomes. No token/time saving is inferred.",
                "Naive timestamps use the declared timezone; local uses a fixed offset at export, including for historical dates.",
                "Missing tables are null (unavailable), not zero."]}
        stable=json.dumps(result,sort_keys=True,ensure_ascii=True)
        result["report_id"]=hashlib.sha256(stable.encode()).hexdigest()[:24]
        return result
    except sqlite3.Error:
        return {"error":"METRICS_READ_FAILED"}
    finally:
        if conn:
            conn.close()
