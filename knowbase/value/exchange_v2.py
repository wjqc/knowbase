"""P3 v2 最小事件包交换（方案 §6.3）：统计与采用证据分别交换。

发送 → 聚合导入 → 回执 → 发送者独立核验后 ACK，全程复用
team.transport.GitData 的「推送后读回比对才 ACK」模式与
value.aggregate 的 agg_events/imports 表。

与 v1 的边界：
- v1 包继续走 exchange.py，不修改已发布包的格式与上限；
- v2 包走单独证据分支（默认 knowbase-value-events-v2），不混入知识检索目录；
- 旧接收者收到 v2 包返回 PEER_SCHEMA_UNSUPPORTED：不导入、不丢弃，
  发送端留待（证据留待发送），绝不把不完整链称通过；
- 导出只读不改 outbox 状态；ACK 只认远端读回的回执，且逐事件哈希一致。
"""

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import aggregate, events_v2
from .events import payload_hash as _payload_hash
from . import outbox as value_outbox

RECEIPT_SCHEMA_V2 = "knowbase-value-receipt-v2"
PACKAGE_PREFIX = "packages/"
RECEIPT_PREFIX = "receipts/"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _canonical(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _package_id(source: dict, target_team: str, entries: list[dict]) -> str:
    core = {"schema": events_v2.SCHEMA_V2, "source": source,
            "target_team": target_team,
            "events": [(e["event"]["event_id"], e["payload_hash"]) for e in entries]}
    return "PKG2-" + hashlib.sha256(_canonical(core).encode("utf-8")).hexdigest()[:20]


# ---------- 打包 ----------

def build_package(raw_events: list[dict], *, source: dict, target_team: str,
                  cursor: dict | None = None) -> dict:
    """把原始事件打成 v2 包：逐条白名单校验，拒绝的单条计入 rejected（可见）。

    返回 package dict，或 {"error": "PACKAGE_TOO_LARGE", ...}。
    """
    entries, rejected = [], []
    for raw in raw_events:
        # 两种输入：export_events 的 {"event": ...} 包装，或裸事件 dict。
        if isinstance(raw, dict) and isinstance(raw.get("event"), dict):
            event, source_key = raw["event"], raw.get("source_key", "")
        else:
            event, source_key = raw, raw.get("source_key", "") if isinstance(raw, dict) else ""
        if not isinstance(event, dict):
            rejected.append({"event_id": "?", "error": "not_a_mapping"})
            continue
        check = events_v2.validate_v2_event(event)
        if "error" in check:
            rejected.append({"event_id": event.get("event_id", "?"), **check})
            continue
        stripped = events_v2.strip_v2_event(event)
        entries.append({"event": stripped, "payload_hash": _payload_hash(stripped),
                        "source_key": source_key})
    entries.sort(key=lambda e: e["event"]["event_id"])
    if len(entries) > events_v2.MAX_EVENTS:
        return {"error": "PACKAGE_TOO_LARGE", "events": len(entries), "max": events_v2.MAX_EVENTS}
    package = {
        "schema": events_v2.SCHEMA_V2,
        "package_id": _package_id(source, target_team, entries),
        "exported_at": _now(),
        "source": source,
        "target_team": target_team,
        "cursor": cursor or {},
        "event_ids": [e["event"]["event_id"] for e in entries],
        "payload_hash": hashlib.sha256(
            _canonical([(e["event"]["event_id"], e["payload_hash"]) for e in entries]
                       ).encode("utf-8")).hexdigest(),
        "events": entries,
        "rejected": rejected,
    }
    text = json.dumps(package, ensure_ascii=False)
    if len(text.encode("utf-8")) > events_v2.MAX_BYTES:
        return {"error": "PACKAGE_TOO_LARGE", "bytes": len(text.encode("utf-8")),
                "max": events_v2.MAX_BYTES}
    return package


def export_events(repo, *, since: str = "", until: str = "",
                  states: tuple[str, ...] = ()) -> dict:
    """从本地 value_outbox 显式导出 v2 包；只读，不改任何事件状态。

    只选 v2 白名单类型；非 v2 事件不在本包内（走 v1 或留待），不是丢弃。
    states 不为空时只导出该状态集合（worker 发送端用 pending/leased，
    避免每轮重发已 ACK 事件）。
    """
    from .. import index
    path = index.db_path(repo)
    if not path.exists():
        return {"error": "REPOSITORY_MISSING"}
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        sql = "SELECT payload_json,payload_hash,source_key FROM value_outbox WHERE state<>'quarantined'"
        params: list = []
        if states:
            sql += f" AND state IN ({','.join('?' * len(states))})"
            params.extend(states)
        if since:
            sql += " AND created_at>=?"
            params.append(since)
        if until:
            sql += " AND created_at<?"
            params.append(until)
        sql += " ORDER BY created_at,event_id"
        raw = []
        for payload_json, _phash, source_key in conn.execute(sql, params).fetchall():
            try:
                event = json.loads(payload_json)
            except ValueError:
                continue
            if isinstance(event, dict) and event.get("event_type") in events_v2.V2_FIELDS:
                raw.append({"event": event, "source_key": source_key})
        identity = _read_identity(conn)
    finally:
        conn.close()
    return build_package(raw, source=identity, target_team=identity.get("team_id", "default"),
                         cursor={"since": since, "until": until})


def _read_identity(conn) -> dict:
    """与 v1 相同的身份来源：value_identity 键值表（一次性团队登记）。"""
    out = {"installation_id": "", "member_binding_id": "", "member_label": "",
           "member_evidence": "", "member_id": "", "team_id": "default"}
    try:
        for key in ("installation_id", "member_binding_id", "member_label", "member_evidence"):
            row = conn.execute("SELECT value FROM value_identity WHERE key=?", (key,)).fetchone()
            out[key] = row[0] if row else ""
    except sqlite3.Error:
        pass
    return out


# ---------- 聚合导入 ----------

def import_package(agg_conn, package: dict, *, team_id: str = "default") -> dict:
    """聚合端导入 v2 包：能力门 → manifest 校验 → 逐事件校验/哈希/幂等/冲突隔离。

    旧协议接收者不走本函数：调用方在 schema 不符时返回
    PEER_SCHEMA_UNSUPPORTED，不导入、不丢弃、不称通过。
    """
    if not isinstance(package, dict) or package.get("schema") != events_v2.SCHEMA_V2:
        return {"error": "PEER_SCHEMA_UNSUPPORTED", "expected": events_v2.SCHEMA_V2}
    items = package.get("events")
    if not isinstance(items, list) or len(items) > events_v2.MAX_EVENTS:
        return {"error": "PACKAGE_MALFORMED", "detail": "events list invalid"}
    if package.get("target_team") and package["target_team"] != team_id:
        return {"error": "TEAM_MISMATCH", "expected": team_id,
                "got": package["target_team"]}
    ids = [e["event"]["event_id"] for e in items
           if isinstance(e, dict) and isinstance(e.get("event"), dict)]
    if sorted(ids) != sorted(package.get("event_ids") or []):
        return {"error": "PACKAGE_MALFORMED", "detail": "event_ids do not match events"}
    expect_hash = hashlib.sha256(_canonical(
        [(e["event"]["event_id"], e["payload_hash"]) for e in items]
        ).encode("utf-8")).hexdigest()
    if package.get("payload_hash") != expect_hash:
        return {"error": "PACKAGE_MALFORMED", "detail": "payload_hash mismatch"}
    source = package.get("source") if isinstance(package.get("source"), dict) else {}
    counts = {"accepted": 0, "duplicate": 0, "conflict": 0, "rejected": 0}
    receipt_events: list[dict] = []
    receipt_id = "RCPT2-" + uuid.uuid4().hex[:20]
    imported_at = _now()
    try:
        agg_conn.execute("BEGIN IMMEDIATE")
        for item in items:
            event = item.get("event") if isinstance(item, dict) else None
            result = _import_one(agg_conn, team_id, item, event, imported_at)
            counts[result["result"]] += 1
            receipt_events.append({"event_id": event.get("event_id", "?")
                                   if isinstance(event, dict) else "?",
                                   "payload_hash": item.get("payload_hash", "")
                                   if isinstance(item, dict) else "",
                                   "result": result["result"]})
        if source.get("installation_id"):
            # 首次 enroll 为准：后续导入只刷新 last_seen，绝不覆盖绑定
            # （与 v1 导入同一规则；归属纠错只走 confirm_member）。
            agg_conn.execute(
                "INSERT INTO installations(installation_id,member_id,member_label,evidence,"
                "member_binding_id,first_seen,last_seen) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(installation_id) DO UPDATE SET last_seen=excluded.last_seen",
                (source["installation_id"], None, source.get("member_label", ""),
                 source.get("member_evidence", ""), source.get("member_binding_id", ""),
                 imported_at, imported_at))
        agg_conn.execute("INSERT OR REPLACE INTO imports(package_id,team_id,receipt_id,counts_json,"
                         "imported_at) VALUES(?,?,?,?,?)",
                         (package.get("package_id") or "unknown", team_id, receipt_id,
                          json.dumps(counts), imported_at))
        agg_conn.commit()
    except Exception:
        agg_conn.rollback()
        raise
    receipt = {"schema": RECEIPT_SCHEMA_V2, "receipt_id": receipt_id,
               "package_id": package.get("package_id", ""), "team_id": team_id,
               "generated_at": imported_at, "counts": counts, "events": receipt_events}
    return {"receipt": receipt, "counts": counts}


def _import_one(conn, team_id, item, event, imported_at) -> dict:
    if not isinstance(event, dict) or not isinstance(item, dict):
        return {"result": "rejected"}
    check = events_v2.validate_v2_event(event)
    if "error" in check:
        return {"result": "rejected"}
    if item.get("payload_hash") != _payload_hash(event):
        return {"result": "rejected"}
    existing = conn.execute("SELECT payload_hash FROM agg_events WHERE team_id=? AND event_id=?",
                            (team_id, event["event_id"])).fetchone()
    if existing:
        return {"result": "duplicate" if existing[0] == item["payload_hash"] else "conflict"}
    payload = event.get("payload", {})
    conn.execute(
        "INSERT INTO agg_events(team_id,event_id,event_type,installation_id,member_binding_id,"
        "host,task_id,occurred_at,session_id,payload_json,payload_hash,source_key,state,imported_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'accepted',?)",
        (team_id, event["event_id"], event["event_type"],
         str(event.get("installation_id", "")), str(event.get("member_binding_id", "")),
         "", str(payload.get("task_id", "")), str(event.get("occurred_at", "")),
         "", json.dumps(event, ensure_ascii=False),
         item["payload_hash"], str(item.get("source_key", "")), imported_at))
    return {"result": "accepted"}


# ---------- 证据分支传输（复用 GitData 读回确认模式） ----------

def send_package(git, package: dict, *, branch: str, retries: int = 3) -> dict:
    """把一包写上证据分支：推送后读回比对一致才算 sent；否则留待重发同包。"""
    from ..team.transport import TransportError
    path = PACKAGE_PREFIX + package["package_id"] + ".json"
    body = json.dumps(package, ensure_ascii=False, sort_keys=True)
    for _ in range(retries):
        try:
            revision = git.fetch(branch)
            if revision is not None:
                existing = git.read(revision, path)
                if existing is not None and json.loads(existing).get("package_id") == package["package_id"]:
                    return {"sent": package["package_id"], "events": len(package["events"]),
                            "confirmed_rev": revision, "already_present": True}
            commit = git.commit(revision, {path: body})
            git.push(branch, commit)
            confirmed = git.fetch(branch)
            remote = git.read(confirmed, path) if confirmed else None
            if remote is not None and json.loads(remote).get("package_id") == package["package_id"]:
                return {"sent": package["package_id"], "events": len(package["events"]),
                        "confirmed_rev": confirmed}
        except TransportError:
            continue
    return {"error": "EVIDENCE_SEND_FAILED", "package_id": package["package_id"]}


def receive_packages(git, agg_conn, *, team_id: str, branch: str) -> dict:
    """聚合端：拉取证据分支，导入未处理包并写回回执；已处理且有回执的跳过。"""
    revision = git.fetch(branch)
    if revision is None:
        return {"imported": 0, "skipped": 0, "receipts": 0, "note": "no_evidence_branch"}
    names = [n for n in git.files(revision) if n.startswith(PACKAGE_PREFIX)]
    imported = skipped = receipts = 0
    writes: dict[str, str] = {}
    for name in sorted(names):
        raw = git.read(revision, name)
        if raw is None:
            continue
        try:
            package = json.loads(raw)
        except ValueError:
            continue
        package_id = package.get("package_id", "")
        receipt_path = RECEIPT_PREFIX + package_id + ".json"
        already = agg_conn.execute(
            "SELECT 1 FROM imports WHERE package_id=? AND team_id=?",
            (package_id, team_id)).fetchone()
        if already and git.read(revision, receipt_path) is not None:
            skipped += 1
            continue
        result = import_package(agg_conn, package, team_id=team_id)
        if "error" in result:
            # 协议/口径问题不阻塞其他包：本包留待，错误数量可见。
            writes.setdefault("errors/" + package_id + ".json", json.dumps(result))
            continue
        writes[receipt_path] = json.dumps(result["receipt"], ensure_ascii=False, sort_keys=True)
        imported += 1
        receipts += 1
    if writes:
        commit = git.commit(revision, writes)
        git.push(branch, commit)
    return {"imported": imported, "skipped": skipped, "receipts": receipts}


def acknowledge_receipts(git, repo, *, branch: str) -> dict:
    """发送端：拉取回执，独立核验 package 事件集合与哈希后才 ACK。

    核验依据本地 outbox 原始事件重算的 v2 剥离哈希；「上传返回成功」
    本身不 ACK；回执缺失/不匹配的事件保持 pending，收据丢失时重发同包。
    """
    from .. import index
    revision = git.fetch(branch)
    if revision is None:
        return {"verified": 0, "acknowledged": 0, "mismatched": [], "missing": [],
                "note": "no_evidence_branch"}
    names = [n for n in git.files(revision) if n.startswith(RECEIPT_PREFIX)]
    conn = sqlite3.connect(index.db_path(repo), timeout=5)
    try:
        verified: list[str] = []
        mismatched: list[str] = []
        missing: list[str] = []
        for name in sorted(names):
            raw = git.read(revision, name)
            if raw is None:
                continue
            try:
                receipt = json.loads(raw)
            except ValueError:
                continue
            if receipt.get("schema") != RECEIPT_SCHEMA_V2:
                continue
            for entry in receipt.get("events", []):
                event_id = entry.get("event_id", "")
                row = conn.execute("SELECT payload_json FROM value_outbox WHERE event_id=?",
                                   (event_id,)).fetchone()
                if not row:
                    missing.append(event_id)
                    continue
                try:
                    original = json.loads(row[0])
                except ValueError:
                    mismatched.append(event_id)
                    continue
                if not isinstance(original, dict) or original.get("event_type") not in events_v2.V2_FIELDS:
                    mismatched.append(event_id)
                    continue
                stripped = events_v2.strip_v2_event(original)
                if _payload_hash(stripped) == entry.get("payload_hash") and \
                        entry.get("result") in ("accepted", "duplicate"):
                    verified.append(event_id)
                else:
                    mismatched.append(event_id)
        acknowledged = 0
        if verified:
            conn.execute("BEGIN IMMEDIATE")
            acknowledged = value_outbox.mark_acknowledged(conn, verified)
            conn.commit()
        return {"verified": len(verified), "acknowledged": acknowledged,
                "mismatched": mismatched, "missing": missing}
    finally:
        conn.close()


# ---------- worker 入口 ----------

def send_pending(repo, git, *, branch: str) -> dict:
    """worker 发送端：一轮导出待发送 v2 事件并推送；无待发送事件时静默跳过。"""
    package = export_events(repo, states=("pending", "leased"))
    if "error" in package:
        return package
    if not package["events"]:
        return {"sent": 0, "note": "no_pending_v2_events"}
    result = send_package(git, package, branch=branch)
    if "error" in result:
        return result
    return {"sent": result["sent"], "events": result["events"],
            "confirmed_rev": result.get("confirmed_rev", "")}


def receive_all(git, agg_db_path: str, *, team_id: str, branch: str) -> dict:
    """worker 聚合端：一轮拉取并导入证据分支上的包，回执写回同分支。"""
    conn = aggregate.connect(agg_db_path)
    try:
        return receive_packages(git, conn, team_id=team_id, branch=branch)
    finally:
        conn.close()
