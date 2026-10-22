"""value_outbox 追加与状态原语。

append 必须运行在调用方的业务事务内（同事务保证：业务记录与事件要么都落库
要么都回滚）；本模块不开 BEGIN、不 commit。发送租约（lease/attempt 递增）
属可选 S1，本版只保留字段与终态原语。
"""

import json
from datetime import datetime, timezone

from . import events as value_events

STATES = frozenset({"pending", "leased", "acknowledged", "quarantined"})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def append(conn, event: dict, *, source_key: str = "") -> dict:
    """幂等追加一条事件。event_id 同内容幂等、异内容冲突；非空 source_key
    （稳定业务键）兜底保证同一关键业务记录只生成一个事件。"""
    if not isinstance(event, dict):
        return {"error": "INVALID_EVENT"}
    if event.get("schema_version") != value_events.SCHEMA_VERSION:
        return {"error": "UNSUPPORTED_SCHEMA", "expected": value_events.SCHEMA_VERSION}
    if event.get("event_type") not in value_events.EVENT_TYPES:
        return {"error": "UNKNOWN_EVENT_TYPE"}
    event_id = event.get("event_id") or ""
    phash = value_events.payload_hash(event)
    if event_id:
        row = conn.execute("SELECT payload_hash FROM value_outbox WHERE event_id=?",
                           (event_id,)).fetchone()
        if row:
            return ({"event_id": event_id, "idempotent": True}
                    if row[0] == phash else {"error": "IDEMPOTENCY_CONFLICT", "event_id": event_id})
    if source_key:
        row = conn.execute("SELECT event_id FROM value_outbox WHERE source_key=?",
                           (source_key,)).fetchone()
        if row:
            return {"event_id": row[0], "idempotent": True, "deduped_by": "source_key"}
    conn.execute(
        "INSERT INTO value_outbox(event_id,schema_version,event_type,source_key,payload_json,"
        "payload_hash,state,attempt,next_attempt_at,lease_owner,lease_until,last_error_code,"
        "created_at,acknowledged_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (event_id, event["schema_version"], event["event_type"], source_key,
         value_events.canonical_payload(event), phash, "pending", 0, "", "", "", "",
         event.get("occurred_at") or _now(), None))
    return {"event_id": event_id, "state": "pending"}


def mark_acknowledged(conn, event_ids) -> int:
    """按接收回执确认事件（A1 汇总机回执核对后调用）；只确认本机已持久记录。"""
    ids = [e for e in event_ids if isinstance(e, str) and e]
    if not ids:
        return 0
    marks = ",".join("?" * len(ids))
    cur = conn.execute(
        "UPDATE value_outbox SET state='acknowledged', acknowledged_at=? "
        f"WHERE event_id IN ({marks}) AND state IN ('pending','leased')", (_now(), *ids))
    return cur.rowcount


def quarantine(conn, event_id: str, error_code: str) -> bool:
    cur = conn.execute(
        "UPDATE value_outbox SET state='quarantined', last_error_code=? "
        "WHERE event_id=? AND state<>'acknowledged'", (error_code[:128], event_id))
    return cur.rowcount > 0


def list_events(conn, *, since: str = "", until: str = "", states=(), limit: int = 0):
    """按窗口列事件（A1 导出/看板读取用）；states 为空表示全部状态。"""
    sql = "SELECT event_id,schema_version,event_type,source_key,payload_json,payload_hash," \
          "state,created_at,acknowledged_at FROM value_outbox WHERE 1=1"
    params: list = []
    if since:
        sql += " AND created_at>=?"
        params.append(since)
    if until:
        sql += " AND created_at<?"
        params.append(until)
    if states:
        sql += f" AND state IN ({','.join('?' * len(states))})"
        params.extend(states)
    sql += " ORDER BY created_at,event_id"
    if limit and limit > 0:
        sql += " LIMIT ?"
        params.append(int(limit))
    rows = conn.execute(sql, params).fetchall()
    return [dict(zip(("event_id", "schema_version", "event_type", "source_key", "payload",
                      "payload_hash", "state", "created_at", "acknowledged_at"), r))
            for r in rows]


def stats(conn) -> dict:
    out = {"total": 0, "by_state": {}, "min_created_at": "", "max_created_at": ""}
    for state, count in conn.execute(
            "SELECT state,COUNT(*) FROM value_outbox GROUP BY state").fetchall():
        out["by_state"][state] = count
        out["total"] += count
    row = conn.execute("SELECT MIN(created_at),MAX(created_at) FROM value_outbox").fetchone()
    out["min_created_at"], out["max_created_at"] = row[0] or "", row[1] or ""
    return out


def decode_payload(payload_json: str) -> dict:
    return json.loads(payload_json)
