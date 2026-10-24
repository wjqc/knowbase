"""事件包导出/导入与接收回执核对（首版文件汇总，不自动上传）。

- export 只读本机 value_outbox，不改变事件状态：导出成功不等于汇总机已接收。
- import 只写汇总分析库（aggregate.connect 拒绝个人 memory.db 目标）；
  按 (team_id,event_id) 幂等，同 ID 异内容隔离为 conflict，哈希不匹配拒绝。
- 回执含逐事件 event_id/payload_hash；源端 acknowledge_receipt 核对通过
  才把本机事件置 acknowledged。文件哈希仅校验完整性，不能认证人员。
"""

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import aggregate, events as value_events, outbox as value_outbox

PACKAGE_SCHEMA = "knowbase.value.events.v1"
PACKAGE_KIND = "value_events_export"
RECEIPT_SCHEMA = "knowbase.value.receipts.v1"
MAX_PACKAGE_BYTES = 64 * 1024 * 1024
MAX_EVENTS_PER_PACKAGE = 20000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_identity(conn) -> dict:
    out = {"installation_id": "", "member_binding_id": "", "member_label": "",
           "member_evidence": "", "member_confirmed_at": ""}
    try:
        for key in out:
            row = conn.execute("SELECT value FROM value_identity WHERE key=?", (key,)).fetchone()
            out[key] = row[0] if row else ""
    except sqlite3.Error:
        pass
    return out


def export_events(repo, output, *, since: str = "", until: str = "") -> dict:
    """显式导出最小事件包；quarantined 不导出；不修改任何事件状态。"""
    from .. import index
    path = Path(output).expanduser()
    db = index.db_path(repo)
    if not db.exists():
        return {"error": "REPOSITORY_MISSING"}
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        sql = "SELECT payload_json,payload_hash,source_key FROM value_outbox WHERE state<>'quarantined'"
        params: list = []
        if since:
            sql += " AND created_at>=?"
            params.append(since)
        if until:
            sql += " AND created_at<?"
            params.append(until)
        sql += " ORDER BY created_at,event_id"
        events_out = []
        for payload_json, phash, source_key in conn.execute(sql, params).fetchall():
            events_out.append({"event": json.loads(payload_json), "payload_hash": phash,
                               "source_key": source_key})
        identity = _read_identity(conn)
    finally:
        conn.close()
    if len(events_out) > MAX_EVENTS_PER_PACKAGE:
        return {"error": "PACKAGE_TOO_LARGE", "events": len(events_out),
                "max": MAX_EVENTS_PER_PACKAGE}
    package = {"schema": PACKAGE_SCHEMA, "package_kind": PACKAGE_KIND,
               "package_id": "PKG-" + uuid.uuid4().hex[:20], "exported_at": _now(),
               "source": identity, "window": {"since": since, "until": until},
               "events": events_out}
    text = json.dumps(package, ensure_ascii=False)
    if len(text.encode("utf-8")) > MAX_PACKAGE_BYTES:
        return {"error": "PACKAGE_TOO_LARGE", "bytes": len(text.encode("utf-8")),
                "max": MAX_PACKAGE_BYTES}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return {"package_id": package["package_id"], "path": str(path),
            "events": len(events_out), "window": {"since": since, "until": until},
            "member_evidence": identity["member_evidence"] or "none"}


def import_package(agg_conn, package_path, *, team_id: str = "default",
                   receipt_out: str = "") -> dict:
    """汇总机导入事件包：幂等、冲突隔离、生成接收回执。"""
    p = Path(package_path).expanduser()
    try:
        raw = p.read_bytes()
    except OSError as exc:
        return {"error": "PACKAGE_UNREADABLE", "detail": type(exc).__name__}
    if len(raw) > MAX_PACKAGE_BYTES:
        return {"error": "PACKAGE_TOO_LARGE"}
    try:
        package = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {"error": "PACKAGE_MALFORMED"}
    if not isinstance(package, dict) or package.get("schema") != PACKAGE_SCHEMA \
            or package.get("package_kind") != PACKAGE_KIND:
        return {"error": "UNSUPPORTED_PACKAGE_SCHEMA", "expected": PACKAGE_SCHEMA}
    items = package.get("events")
    if not isinstance(items, list) or len(items) > MAX_EVENTS_PER_PACKAGE:
        return {"error": "PACKAGE_MALFORMED", "detail": "events list invalid"}
    source = package.get("source") if isinstance(package.get("source"), dict) else {}
    receipt_events: list[dict] = []
    counts = {"accepted": 0, "duplicate": 0, "conflict": 0, "rejected": 0}
    receipt_id = "RCPT-" + uuid.uuid4().hex[:20]
    imported_at = _now()
    try:
        agg_conn.execute("BEGIN IMMEDIATE")
        for item in items:
            event = item.get("event") if isinstance(item, dict) else None
            event_id = event.get("event_id") if isinstance(event, dict) else ""
            result = _import_one(agg_conn, team_id, item, event, imported_at)
            counts[result["result"]] += 1
            receipt_events.append({"event_id": event_id or "?",
                                   "payload_hash": item.get("payload_hash", "")
                                   if isinstance(item, dict) else "",
                                   "result": result["result"]})
        if source.get("installation_id"):
            # 首次 enroll 记录链（Phase 3-A）：同一安装实例的绑定以首次导入
            # 为准，后续导入只刷新 last_seen，绝不覆盖 member_binding_id/
            # evidence/member_label/first_seen；归属变更只走 confirm_member
            # 纠错入口（带留痕），不靠重复导入改写。
            agg_conn.execute(
                "INSERT INTO installations(installation_id,member_id,member_label,evidence,"
                "member_binding_id,first_seen,last_seen) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(installation_id) DO UPDATE SET last_seen=excluded.last_seen",
                (source["installation_id"], None, source.get("member_label", ""),
                 source.get("member_evidence", ""), source.get("member_binding_id", ""),
                 imported_at, imported_at))
        agg_conn.execute("INSERT OR REPLACE INTO imports(package_id,team_id,receipt_id,counts_json,"
                         "imported_at) VALUES(?,?,?,?,?)",
                         (package.get("package_id") or f"unknown:{p.name}", team_id, receipt_id,
                          json.dumps(counts), imported_at))
        agg_conn.commit()
    except Exception:
        agg_conn.rollback()
        raise
    receipt = {"schema": RECEIPT_SCHEMA, "receipt_id": receipt_id,
               "package_id": package.get("package_id", ""), "team_id": team_id,
               "generated_at": imported_at, "counts": counts, "events": receipt_events}
    if receipt_out:
        out_path = Path(receipt_out).expanduser()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"receipt": receipt, "counts": counts, "receipt_path": receipt_out or ""}


def _import_one(conn, team_id, item, event, imported_at) -> dict:
    if not isinstance(event, dict):
        return {"result": "rejected"}
    if event.get("schema_version") != PACKAGE_SCHEMA or \
            event.get("event_type") not in value_events.EVENT_TYPES or \
            not isinstance(event.get("event_id"), str) or not event["event_id"]:
        return {"result": "rejected"}
    if not isinstance(item, dict) or \
            item.get("payload_hash") != value_events.payload_hash(event):
        return {"result": "rejected"}
    # 不信任来源端自校验：导入侧重跑完整形状与 payload 白名单，
    # 额外顶层键/嵌套对象/额外 payload 字段（如正文、查询）整条拒绝。
    try:
        value_events.validate_event_shape(event)
        value_events.validate_payload(event["event_type"], event.get("payload"))
    except ValueError:
        return {"result": "rejected"}
    existing = conn.execute("SELECT payload_hash FROM agg_events WHERE team_id=? AND event_id=?",
                            (team_id, event["event_id"])).fetchone()
    if existing:
        return {"result": "duplicate" if existing[0] == item["payload_hash"] else "conflict"}
    conn.execute(
        "INSERT INTO agg_events(team_id,event_id,event_type,installation_id,member_binding_id,"
        "host,task_id,occurred_at,session_id,payload_json,payload_hash,source_key,state,imported_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'accepted',?)",
        (team_id, event["event_id"], event["event_type"],
         str(event.get("installation_id", "")), str(event.get("member_binding_id", "")),
         str(event.get("host", "")), str(event.get("task_id", "")),
         str(event.get("occurred_at", "")), str(event.get("session_id", "")),
         json.dumps(event, ensure_ascii=False),
         item["payload_hash"], str(item.get("source_key", "")), imported_at))
    return {"result": "accepted"}


def acknowledge_receipt(repo, receipt) -> dict:
    """源端核对回执：逐事件哈希一致才置 acknowledged；不确认缺失/不匹配项。"""
    from .. import index
    if not isinstance(receipt, dict) or receipt.get("schema") != RECEIPT_SCHEMA:
        return {"error": "UNSUPPORTED_RECEIPT_SCHEMA"}
    conn = sqlite3.connect(index.db_path(repo), timeout=5)
    try:
        verified: list[str] = []
        mismatched: list[str] = []
        missing: list[str] = []
        for entry in receipt.get("events", []):
            event_id = entry.get("event_id", "")
            row = conn.execute("SELECT payload_hash FROM value_outbox WHERE event_id=?",
                               (event_id,)).fetchone()
            if not row:
                missing.append(event_id)
            elif row[0] == entry.get("payload_hash") and entry.get("result") in ("accepted", "duplicate"):
                verified.append(event_id)
            else:
                mismatched.append(event_id)
        acknowledged = 0
        if verified:
            conn.execute("BEGIN IMMEDIATE")
            acknowledged = value_outbox.mark_acknowledged(conn, verified)
            conn.commit()
        return {"verified": len(verified), "acknowledged": acknowledged,
                "mismatched": mismatched, "missing": missing,
                "receipt_id": receipt.get("receipt_id", "")}
    finally:
        conn.close()
